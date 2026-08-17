"""Worker-side Host-policy enforcement: Policy hooks, Memory injection, Worker MCP.

The Runtime's worker loop (deepseek/qwen/… via the Anthropic Messages SDK)
enforces the Host operator's policy at three worker seams — the same
deny/allow/context decisions the Host harness would deliver. Three orthogonal
capabilities live here, in three clearly delimited sections:

* **Policy hooks** — operator-authored hooks sourced from the Host's settings
  surfaces (Claude: managed/user/project/local settings files; Codex: the CLI's
  `config.toml` hook tables). They fire at PreToolUse, PostToolUse and Stop with
  Claude-canonical tool names, so existing matchers and scripts bind. Plugin
  hooks are NEVER Policy hooks: workers do not load plugins.
* **Memory injection** — a delimited block carrying Memory-file content
  (CLAUDE.md/AGENTS.md and their import companions) into the worker Thread:
  prepended to the first user turn on a fresh Job, and delivered alongside a tool
  result when a touched subdirectory's Memory file loads lazily.
* **Worker MCP** — per-Job stdio connections to the Host's configured MCP
  servers, whose tools are advertised alongside the native roster and governed by
  the same Policy hooks.

The Job-runtime policy layer — ``Policy.build`` and everything it drives (hook
firing, Memory injection, MCP) — sits OUTSIDE the Registry's exception
normalization and must NEVER raise: a discovery/translation/synthesis/dispatch
failure degrades to a fail-open default (allow/continue) or an error-flavored
observation, logged with a stable grep-able ``[policy]`` prefix. The lone
exception is ``load_policy_settings``, the dispatch-boundary loader that
validates the per-Host ``settings.json`` toggle file and raises
``PolicySettingsError`` on a malformed file (default OFF when absent; ADR 0016
amended 0.7.0). See ADR 0016.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from chinamax import ChinamaxError, state
from chinamax.confinement import resolve_in_workspace
from chinamax.host import Host, HostContext
from chinamax.liveness import emit_event
from chinamax.tools.bash import TERMINATE_GRACE_S, TailBuffer, _drain, truncate_tail
from chinamax.tools.patch import parse_patch

#: The stable, grep-able prefix on every policy progress line (ADR 0016). A
#: broken hook weakens, never silently drops — every fail-open downgrade and
#: every Stop block carries this prefix in the progress log.
POLICY_LOG_PREFIX = "[policy] "

#: A logger is ``(message) -> None``. The loop supplies one that mirrors into the
#: Job log through the reporter, or falls back to the structured warning channel
#: on the record-less ``exec`` path where the reporter silently swallows.
PolicyLog = Callable[[str], None]

#: Default per-hook timeout when a hook source does not set one (harness parity).
DEFAULT_HOOK_TIMEOUT_S = 60.0

#: Bounded MCP lifecycle timeouts (overridable for tests). A dead or silent
#: server yields an error-flavored tool_result, never a hung Job.
_MCP_CONNECT_TIMEOUT_S = float(os.environ.get("CHINAMAX_MCP_CONNECT_TIMEOUT_S", "30") or 30)
_MCP_CALL_TIMEOUT_S = float(os.environ.get("CHINAMAX_MCP_CALL_TIMEOUT_S", "60") or 60)
_MCP_CLOSE_TIMEOUT_S = float(os.environ.get("CHINAMAX_MCP_CLOSE_TIMEOUT_S", "10") or 10)

#: Test seam: override the platform-specific managed-settings path so no test
#: reads the operator's real managed settings (ADR 0016). Discovery roots are
#: overridable; the operator's real settings are never touched by the suite.
MANAGED_SETTINGS_VARIABLE = "CHINAMAX_MANAGED_SETTINGS"


def canonical_json(obj: object) -> str:
    """Serialize deterministically — the repo's canonical-JSON precedent (doctor).

    ONE shared serializer: sorted keys and separator-tight, so the MCP tool
    snapshot's assertion/persistence form is byte-identical across turns. The
    SDK is handed the list of dicts (same object → same bytes); this string is
    the assertion form, not the wire form.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ════════════════════════════════════════════════════════════════════════════
# Section 0 — Policy toggle settings (the per-Host settings.json)
# ════════════════════════════════════════════════════════════════════════════

#: The per-Host toggle file, resolved at ``HostContext.state_root`` / this name.
#: Its basename collides with Claude's own ``~/.claude/settings.json`` hooks
#: source — a DIFFERENT file with a different schema — so every operator-facing
#: surface prints its FULL path.
SETTINGS_FILENAME = "settings.json"

#: The three orthogonal Policy toggles, each governing its ENTIRE ADR 0016
#: feature. Default OFF: an absent file or key resolves the toggle to False.
_POLICY_TOGGLES = ("memory", "hooks", "mcp")


class PolicySettingsError(ChinamaxError):
    """A malformed ``settings.json`` — the one deliberate dispatch-boundary raise.

    Raised only by :func:`load_policy_settings` (dispatch/exec) and the settings
    writer, NEVER by :meth:`Policy.build` or anything downstream, whose never-raise
    contract is intact. Subclasses ``ChinamaxError`` so the CLI boundary renders
    it as ``chinamax: <msg>`` and exits 1 until the operator fixes the file.
    """


@dataclass(frozen=True)
class PolicySettings:
    """One Host's three resolved Policy toggles (default all OFF)."""

    memory: bool = False
    hooks: bool = False
    mcp: bool = False


def _settings_path(host_context: HostContext) -> Path:
    """The Host's toggle file, beside the per-workspace state directories."""
    return host_context.state_root / SETTINGS_FILENAME


def _coerce_policy_settings(data: object, path: Path) -> PolicySettings:
    """Validate a decoded settings document into resolved toggles, or raise.

    Absent key ⇒ OFF; unknown extra keys are ignored (additive schema). A
    non-object top level or ``policy`` value, or a non-boolean toggle value
    (JSON ``null`` included — present-but-null is an error, absent is OFF), is
    malformed.

    Args:
        data: The decoded JSON document.
        path: The settings file, named in any error.

    Returns:
        The resolved toggles.

    Raises:
        PolicySettingsError: On a non-object document/``policy`` value, or a
            non-boolean toggle value.
    """
    if not isinstance(data, dict):
        raise PolicySettingsError(f"policy settings {path} must be a JSON object")
    policy = data.get("policy", {})
    if not isinstance(policy, dict):
        raise PolicySettingsError(f"policy settings {path}: 'policy' must be an object")
    values: dict[str, bool] = {}
    for name in _POLICY_TOGGLES:
        if name not in policy:
            values[name] = False
            continue
        value = policy[name]
        if not isinstance(value, bool):
            raise PolicySettingsError(
                f"policy settings {path}: 'policy.{name}' must be true or false, "
                f"not {value!r}"
            )
        values[name] = value
    return PolicySettings(**values)


def load_policy_settings(host_context: HostContext) -> PolicySettings:
    """Resolve the Host's Policy toggles, raising on a malformed file.

    The dispatch/exec reader — the ONE dispatch-boundary raise. An absent file or
    key resolves OFF; an unparseable file, a non-object document, a non-boolean
    toggle, an unreadable file, or a directory at the path raises
    ``PolicySettingsError`` naming the file.

    Args:
        host_context: The Host whose state root holds the file.

    Returns:
        The resolved toggles (all OFF when the file is absent).

    Raises:
        PolicySettingsError: On any malformed settings file.
    """
    path = _settings_path(host_context)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PolicySettings()
    except OSError as exc:
        raise PolicySettingsError(f"policy settings {path} is unreadable: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PolicySettingsError(f"policy settings {path} is not valid JSON: {exc}") from exc
    return _coerce_policy_settings(data, path)


def inspect_policy_settings(host_context: HostContext) -> tuple[PolicySettings, bool]:
    """Resolve the toggles tolerantly for status/setup — never raises.

    The tolerant twin of :func:`load_policy_settings`: a malformed file becomes a
    reported flag with OFF defaults, so status keeps listing Jobs and setup can
    report the repair. No caller branches on a mode flag; this ONE wrapper is the
    tolerant reader, so setup/status never grow private try/except.

    Args:
        host_context: The Host whose state root holds the file.

    Returns:
        ``(settings, malformed)`` — OFF defaults with ``malformed`` True on a bad
        file.
    """
    try:
        return load_policy_settings(host_context), False
    except PolicySettingsError:
        return PolicySettings(), True


def _read_settings_document(path: Path) -> dict:
    """The existing well-formed settings document, or ``{}`` to rebuild from scratch."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_policy_settings(
    host_context: HostContext, updates: Mapping[str, bool]
) -> PolicySettings:
    """Persist a subset toggle update to the Host's ``settings.json``.

    A well-formed file keeps its unknown top-level keys AND its existing toggle
    block, updating ONLY the named toggles; an absent or malformed file is
    (re)built from all-OFF defaults overlaid with the named toggles (the fresh
    write and the repair). Publishes atomically via the state-write helper,
    creating the state root first. A DIRECTORY at the path is NOT repairable —
    ``os.replace`` cannot replace a directory — so this raises and setup asks for
    manual removal instead.

    Args:
        host_context: The Host whose state root holds the file.
        updates: The named toggles to set (a subset of memory/hooks/mcp).

    Returns:
        The resolved toggles after the write.

    Raises:
        PolicySettingsError: When a directory sits at the settings path.
    """
    path = _settings_path(host_context)
    if path.is_dir():
        raise PolicySettingsError(
            f"policy settings {path} is a directory, not a file; remove it and "
            "re-run setup"
        )
    _, malformed = inspect_policy_settings(host_context)
    if malformed or not path.exists():
        document: dict = {}
        policy_block: dict = {name: False for name in _POLICY_TOGGLES}
    else:
        document = _read_settings_document(path)
        existing = document.get("policy")
        policy_block = dict(existing) if isinstance(existing, dict) else {}
    for name, value in updates.items():
        policy_block[name] = bool(value)
    document["policy"] = policy_block
    state.make_dir(host_context.state_root)
    state._write_file(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return _coerce_policy_settings(document, path)


# ════════════════════════════════════════════════════════════════════════════
# Section 1 — Policy hooks
# ════════════════════════════════════════════════════════════════════════════

#: Native-tool → Claude-canonical name. ``apply_patch`` and ``report_result``
#: are handled specially (per-file Edit synthesis; the Stop event) and are not
#: in this table.
_TOOL_NAMES = {
    "bash": "Bash",
    "read_file": "Read",
    "write_file": "Write",
    "str_replace_edit": "Edit",
    "grep": "Grep",
    "glob": "Glob",
    "list_dir": "Glob",
}

#: Native tools whose ``path`` input names a workspace file, so a successful call
#: is a lazy-Memory touch (bash and MCP paths are out of scope — uninferable).
_PATH_TOOLS = {
    "read_file",
    "write_file",
    "str_replace_edit",
    "list_dir",
    "grep",
    "glob",
}


def translate_tool(name: str, value: object) -> tuple[str, dict]:
    """Present one native tool_use with its Claude-canonical name and keys.

    Full translation, so existing matchers and scripts bind: unmapped native
    keys ride along unchanged (matchers see supersets, never lose fields). A
    non-dict input is presented as an empty ``tool_input`` rather than raising —
    the policy layer never raises.

    Args:
        name: The native tool name.
        value: The native tool input.

    Returns:
        ``(claude_name, translated_input)``.
    """
    claude = _TOOL_NAMES.get(name, name)
    payload: dict = dict(value) if isinstance(value, dict) else {}
    if name in {"read_file", "write_file", "str_replace_edit"} and "path" in payload:
        payload["file_path"] = payload["path"]
    elif name == "grep" and "include" in payload:
        payload["glob"] = payload["include"]
    elif name == "list_dir":
        payload.setdefault("pattern", "*")
    return claude, payload


@dataclass(frozen=True)
class HookSpec:
    """One operator-authored Policy hook, normalized across both Hosts."""

    command: str | None
    command_windows: str | None
    matcher: str | None
    timeout_s: float
    source: str


@dataclass(frozen=True)
class HookConfig:
    """Every Policy hook for one Job, keyed by event, in source+config order."""

    disabled: bool
    pre: tuple[HookSpec, ...]
    post: tuple[HookSpec, ...]
    stop: tuple[HookSpec, ...]

    def for_event(self, event: str) -> tuple[HookSpec, ...]:
        return {"PreToolUse": self.pre, "PostToolUse": self.post, "Stop": self.stop}[event]


@dataclass(frozen=True)
class PreResult:
    """A PreToolUse outcome: allow (with optional contexts) or deny (with reason)."""

    allowed: bool
    reason: str | None = None
    contexts: tuple[str, ...] = ()


@dataclass(frozen=True)
class StopResult:
    """A Stop outcome: blocked (with reason) or not."""

    blocked: bool
    reason: str = ""


def _managed_settings_path() -> Path | None:
    """The platform-specific managed-settings location, or a test override."""
    override = os.environ.get(MANAGED_SETTINGS_VARIABLE, "").strip()
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", "").strip()
        base = Path(program_data) if program_data else Path("C:/ProgramData")
        return base / "ClaudeCode" / "managed-settings.json"
    return Path("/etc/claude-code/managed-settings.json")


def _read_json(path: Path) -> dict | None:
    """Read a JSON object, returning None on any failure (never raises)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_toml(path: Path) -> dict | None:
    """Read a TOML document, returning None on any failure (never raises)."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None


def project_root(workspace: Path) -> Path:
    """Nearest ancestor of the workspace (inclusive) with ``.claude/`` or ``.git``.

    The settings project root deliberately diverges from the Job workspace root
    in nested-repo layouts (ADR 0016). Falls back to the workspace itself.
    """
    for candidate in (workspace, *workspace.parents):
        if (candidate / ".claude").exists() or (candidate / ".git").exists():
            return candidate
    return workspace


def _claude_hook_specs(data: dict, event: str, source: str) -> list[HookSpec]:
    """Extract one Claude settings source's hook groups for an event."""
    specs: list[HookSpec] = []
    groups = data.get("hooks")
    if not isinstance(groups, dict):
        return specs
    for group in groups.get(event, []) or []:
        if not isinstance(group, dict):
            continue
        matcher = group.get("matcher")
        matcher = matcher if isinstance(matcher, str) else None
        for hook in group.get("hooks", []) or []:
            if not isinstance(hook, dict):
                continue
            if hook.get("type") not in (None, "command"):
                continue
            command = hook.get("command")
            if not isinstance(command, str) or not command:
                continue
            specs.append(
                HookSpec(
                    command=command,
                    command_windows=None,
                    matcher=matcher,
                    timeout_s=_hook_timeout(hook.get("timeout")),
                    source=source,
                )
            )
    return specs


def _codex_hook_specs(data: dict, event: str, source: str) -> list[HookSpec]:
    """Extract Codex ``config.toml`` ``[[hooks.<event>]]`` tables for an event.

    Deliberately runs ALL listed hooks: the worker ignores ``hooks.state``
    trusted_hash entries and the ``[features] hooks`` flag, because
    operator-authored ``config.toml`` entries are the consent boundary (ADR 0016
    records this divergence from the Codex CLI's own gating).
    """
    specs: list[HookSpec] = []
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return specs
    for table in hooks.get(event, []) or []:
        if not isinstance(table, dict):
            continue
        command = table.get("command")
        command_windows = table.get("commandWindows")
        command = command if isinstance(command, str) and command else None
        command_windows = (
            command_windows if isinstance(command_windows, str) and command_windows else None
        )
        if command is None and command_windows is None:
            continue
        matcher = table.get("matcher")
        specs.append(
            HookSpec(
                command=command,
                command_windows=command_windows,
                matcher=matcher if isinstance(matcher, str) else None,
                timeout_s=_hook_timeout(table.get("timeout")),
                source=source,
            )
        )
    return specs


def _hook_timeout(value: object) -> float:
    """A hook's timeout in seconds, defaulting when unset or malformed."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_HOOK_TIMEOUT_S
    return float(value) if value > 0 else DEFAULT_HOOK_TIMEOUT_S


def discover_hooks(workspace: Path, host_context: HostContext, log: PolicyLog) -> HookConfig:
    """Discover every Policy hook for one Job — settings only, parsed once.

    Claude: managed → user → project → local, in that source order. Codex: the
    single ``config.toml``. An unreadable/malformed source contributes zero hooks
    (logged) and cannot conceal another source's ``disableAllHooks``.
    """
    if host_context.host is Host.CODEX:
        return _discover_codex_hooks(host_context, log)
    return _discover_claude_hooks(workspace, host_context, log)


def _discover_claude_hooks(
    workspace: Path, host_context: HostContext, log: PolicyLog
) -> HookConfig:
    host_home = host_context.keys_path.parent
    root = project_root(workspace)
    sources: list[tuple[str, Path | None]] = [
        ("managed", _managed_settings_path()),
        ("user", host_home / "settings.json"),
        ("project", root / ".claude" / "settings.json"),
        ("local", root / ".claude" / "settings.local.json"),
    ]
    pre: list[HookSpec] = []
    post: list[HookSpec] = []
    stop: list[HookSpec] = []
    disabled = False
    for label, path in sources:
        if path is None or not path.exists():
            continue
        data = _read_json(path)
        if data is None:
            log(f"settings source {label} at {path} is unreadable or malformed; skipped")
            continue
        if data.get("disableAllHooks") is True:
            log(f"disableAllHooks in the {label} settings source: no Policy hooks for this Job")
            disabled = True
        pre.extend(_claude_hook_specs(data, "PreToolUse", label))
        post.extend(_claude_hook_specs(data, "PostToolUse", label))
        stop.extend(_claude_hook_specs(data, "Stop", label))
    if disabled:
        return HookConfig(disabled=True, pre=(), post=(), stop=())
    return HookConfig(False, tuple(pre), tuple(post), tuple(stop))


def _discover_codex_hooks(host_context: HostContext, log: PolicyLog) -> HookConfig:
    config = host_context.keys_path.parent / "config.toml"
    if not config.exists():
        return HookConfig(False, (), (), ())
    data = _read_toml(config)
    if data is None:
        log(f"Codex config.toml at {config} is unreadable or malformed; no Policy hooks")
        return HookConfig(False, (), (), ())
    return HookConfig(
        False,
        tuple(_codex_hook_specs(data, "PreToolUse", "codex")),
        tuple(_codex_hook_specs(data, "PostToolUse", "codex")),
        tuple(_codex_hook_specs(data, "Stop", "codex")),
    )


def _matches(spec: HookSpec, tool_name: str, log: PolicyLog) -> bool:
    """Fully-match a hook group's matcher regex against the translated tool name.

    A missing/empty matcher or ``"*"`` matches every tool; an invalid regex skips
    that group (fail-open, logged).
    """
    matcher = spec.matcher
    if matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool_name) is not None
    except re.error as error:
        log(f"invalid hook matcher {matcher!r} ({error}); group skipped")
        return False


@dataclass
class _HookOutput:
    """One hook process's outcome."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    spawn_error: str | None = None


def _hook_argv(spec: HookSpec, log: PolicyLog) -> list[str] | None:
    """Build the argv for one hook, resolving bash/cmd per Platform.

    On Windows a Codex ``commandWindows`` is a cmd.exe command line run via
    cmd.exe (never routed through the Git Bash resolver); a bash-shaped
    ``command`` resolves bash via the shared Git for Windows probe. A resolution
    failure is logged distinctly from a generic crash and returns None.
    """
    if sys.platform == "win32" and spec.command_windows is not None:
        return ["cmd", "/d", "/c", spec.command_windows]
    if spec.command is None:
        # Only a Codex commandWindows-only hook on a non-Windows Platform.
        log(f"hook from {spec.source} has no POSIX command; skipped")
        return None
    if sys.platform == "win32":
        resolved = state.windows_tool_path("bash")
        if resolved is None:
            log(f"bash resolution failed for a {spec.source} hook; skipped (Windows Git Bash absent)")
            return None
        return [resolved, "-c", spec.command]
    return ["bash", "-c", spec.command]


def _run_hook(spec: HookSpec, payload: dict, cwd: Path, env: dict, log: PolicyLog) -> _HookOutput:
    """Run one hook process, bounded, mirroring ``run_bash``'s spawn/kill seams.

    stdin is the harness JSON payload; stdout/stderr capture is BOUNDED, reusing
    the bash runner's tail-buffer drain. A timeout kills the whole process tree
    via the existing tree-kill seam.
    """
    argv = _hook_argv(spec, log)
    if argv is None:
        return _HookOutput(None, "", "", False, spawn_error="unresolved hook command")
    options: dict = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(argv, **options)
    except OSError as error:
        return _HookOutput(None, "", "", False, spawn_error=f"{type(error).__name__}: {error}")
    start_time = state.read_pid_start_time(process.pid)
    stdout, stderr = TailBuffer(), TailBuffer()
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, stdout), daemon=sys.platform == "win32"),
        threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=sys.platform == "win32"),
    ]
    for reader in readers:
        reader.start()
    try:
        process.stdin.write(canonical_json(payload).encode("utf-8"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
    timed_out = False
    try:
        exit_code = process.wait(timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        state.terminate_tree(process.pid, start_time, TERMINATE_GRACE_S, TERMINATE_GRACE_S)
    for reader in readers:
        reader.join(TERMINATE_GRACE_S if sys.platform == "win32" else None)
    return _HookOutput(exit_code, stdout.text(), stderr.text(), timed_out)


def _decode_hook_stdout(text: str) -> dict | None:
    """Parse a hook's stdout JSON object, or None (fail-open on anything else)."""
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _hook_context(data: dict) -> str | None:
    """Pull ``hookSpecificOutput.additionalContext`` when present and non-empty."""
    extra = data.get("hookSpecificOutput")
    if isinstance(extra, dict):
        context = extra.get("additionalContext")
        if isinstance(context, str) and context.strip():
            return context
    return None


# ════════════════════════════════════════════════════════════════════════════
# Section 2 — Memory injection
# ════════════════════════════════════════════════════════════════════════════

#: The FIXED versioned sentinel pair. The block writer and the scanner share this
#: one format definition; a content line matching a delimiter is escaped at
#: injection time so no imported payload can forge or truncate a block.
_MEMORY_OPEN = "<<<CHINAMAX-MEMORY:v1>>>"
_MEMORY_CLOSE = "<<<CHINAMAX-MEMORY-END:v1>>>"
_MEMORY_FILE_PREFIX = "<<<CHINAMAX-MEMORY-FILE:v1 "
_MEMORY_FILE_SUFFIX = ">>>"
_MEMORY_INTRO = (
    "The operator's Host memory files below apply to this workspace. They are "
    "injected context, not something you must re-read."
)
#: A zero-width space escaping a content line that would collide with a delimiter.
_MEMORY_ESCAPE = "\u200b"

#: Per-file injection cap: a generous head-truncation, so an unbounded import
#: chain cannot 400 the first provider request before the Job can recover.
_MEMORY_FILE_CAP = 100_000
#: Max @-import hop depth (the repo's AGENTS.md→CLAUDE.md stub must resolve).
_MEMORY_IMPORT_DEPTH = 5

_IMPORT_RE = re.compile(r"(?<!\S)@(\S+)")


@dataclass(frozen=True)
class MemoryFile:
    """One discovered Memory file: its real path and (bounded) content."""

    path: str
    content: str


def _memory_filenames(host_context: HostContext) -> tuple[str, ...]:
    """The Memory-file basenames for one Host, most-specific last."""
    if host_context.host is Host.CODEX:
        return ("AGENTS.md",)
    return ("CLAUDE.md", "CLAUDE.local.md")


def _host_global_memory(host_context: HostContext) -> Path:
    """The Host-global Memory file (`~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md`)."""
    home = host_context.keys_path.parent
    return home / ("AGENTS.md" if host_context.host is Host.CODEX else "CLAUDE.md")


def _is_excluded_memory(path: Path, host_context: HostContext) -> bool:
    """Exclude the Claude memory store: MEMORY.md and memory/ under projects/.

    Never a workspace directory that merely happens to be named ``memory/`` —
    only the store rooted at ``<host home>/projects/`` (Claude Host).
    """
    if host_context.host is Host.CODEX:
        return False
    projects = host_context.keys_path.parent / "projects"
    try:
        relative = path.resolve().relative_to(projects.resolve())
    except (OSError, ValueError):
        return False
    parts = relative.parts
    if len(parts) < 2:
        return False
    # parts[0] is <slug>; the store is <slug>/MEMORY.md or <slug>/memory/...
    tail = parts[1:]
    if tail[-1] == "MEMORY.md":
        return True
    return "memory" in tail[:-1]


def _read_memory_text(path: Path) -> str | None:
    """Read a Memory file as text, head-truncated past the cap (never raises)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    encoded = raw.encode("utf-8")
    if len(encoded) <= _MEMORY_FILE_CAP:
        return raw
    head = encoded[:_MEMORY_FILE_CAP].decode("utf-8", errors="replace")
    return head + f"\n[... chinamax truncated this memory file to {_MEMORY_FILE_CAP} bytes ...]"


def _resolve_imports(content: str, base: Path) -> list[str]:
    """Collect ``@``-import targets from one file's body, fences/spans ignored.

    A simple line-state scanner (``` / ~~~ fenced regions and inline backtick
    spans), not a full Markdown parser (ADR 0016). ``~`` expands; a target is
    resolved relative to the importing file.
    """
    targets: list[str] = []
    in_fence = False
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Strip inline backtick spans so an @ inside `code` is ignored.
        without_spans = re.sub(r"`[^`]*`", "", line)
        for match in _IMPORT_RE.finditer(without_spans):
            targets.append(match.group(1))
    resolved: list[str] = []
    for target in targets:
        expanded = os.path.expanduser(target)
        candidate = Path(expanded) if os.path.isabs(expanded) else (base.parent / expanded)
        resolved.append(str(candidate))
    return resolved


def _collect_memory_file(
    path: Path,
    host_context: HostContext,
    seen: set[str],
    out: list[MemoryFile],
    log: PolicyLog,
    depth: int,
) -> None:
    """Add one Memory file and recursively resolve its imports (cycle-guarded)."""
    try:
        real = str(path.resolve())
    except OSError:
        return
    if real in seen:
        return
    if _is_excluded_memory(path, host_context):
        return
    if not path.is_file():
        return
    text = _read_memory_text(path)
    if text is None:
        log(f"memory file {path} is unreadable; skipped")
        return
    seen.add(real)
    out.append(MemoryFile(path=real, content=text))
    if depth >= _MEMORY_IMPORT_DEPTH:
        return
    for target in _resolve_imports(text, path):
        target_path = Path(target)
        try:
            already = str(target_path.resolve()) in seen
        except OSError:
            already = target in seen
        if already:
            log(f"skipping cyclic/duplicate @import {target}")
            continue
        _collect_memory_file(target_path, host_context, seen, out, log, depth + 1)


def discover_memory(workspace: Path, host_context: HostContext, log: PolicyLog) -> list[MemoryFile]:
    """Discover the fresh-Job Memory chain: global + ancestor chain + imports.

    The Host-global file, then every Memory file from filesystem root down the
    ancestor chain to the Job workspace (inclusive) plus ``CLAUDE.local.md``
    siblings on Claude, with recursive ``@``-import resolution. The Claude memory
    store is excluded.
    """
    files: list[MemoryFile] = []
    seen: set[str] = set()
    _collect_memory_file(_host_global_memory(host_context), host_context, seen, files, log, 0)
    chain = [*reversed(workspace.parents), workspace]
    names = _memory_filenames(host_context)
    for directory in chain:
        for name in names:
            _collect_memory_file(directory / name, host_context, seen, files, log, 0)
    return files


def _escape_memory_line(line: str) -> str:
    """Neutralize a content line that would forge or truncate a block delimiter."""
    if (
        line == _MEMORY_OPEN
        or line == _MEMORY_CLOSE
        or (line.startswith(_MEMORY_FILE_PREFIX) and line.endswith(_MEMORY_FILE_SUFFIX))
    ):
        return _MEMORY_ESCAPE + line
    return line


def build_memory_block(files: Iterable[MemoryFile]) -> str:
    """Assemble one delimited Memory-injection block with per-file path headers."""
    lines = [_MEMORY_OPEN, _MEMORY_INTRO]
    for memory in files:
        lines.append(f"{_MEMORY_FILE_PREFIX}{memory.path}{_MEMORY_FILE_SUFFIX}")
        for line in memory.content.splitlines():
            lines.append(_escape_memory_line(line))
    lines.append(_MEMORY_CLOSE)
    return "\n".join(lines)


def scan_injected_paths(messages: list[dict]) -> set[str]:
    """Derive the already-injected Memory-file path set from replayed messages.

    Matches STRUCTURAL injection blocks only (exact delimiter lines at message
    text-block boundaries), NEVER a substring search over message content — this
    repo's own tests contain the marker text, and reading them via a tool must
    not poison the set.
    """
    injected: set[str] = set()
    for message in messages:
        if message.get("role") != "user":
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str) or _MEMORY_OPEN not in text:
                continue
            in_block = False
            for line in text.splitlines():
                if line == _MEMORY_OPEN:
                    in_block = True
                elif line == _MEMORY_CLOSE:
                    in_block = False
                elif (
                    in_block
                    and line.startswith(_MEMORY_FILE_PREFIX)
                    and line.endswith(_MEMORY_FILE_SUFFIX)
                ):
                    injected.add(line[len(_MEMORY_FILE_PREFIX) : -len(_MEMORY_FILE_SUFFIX)])
    return injected


# ════════════════════════════════════════════════════════════════════════════
# Section 3 — Worker MCP
# ════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class MCPServerConfig:
    """One discovered stdio MCP server."""

    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]
    cwd: str | None


def _parse_mcp_entry(name: str, entry: object) -> MCPServerConfig | None:
    """Validate one server config entry; None for disabled/malformed/non-stdio."""
    if not isinstance(entry, dict):
        return None
    if entry.get("disabled") is True:
        return None
    transport = entry.get("type")
    if transport not in (None, "stdio"):
        return None
    if entry.get("url"):
        return None
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        return None
    args = entry.get("args")
    args = tuple(str(a) for a in args) if isinstance(args, list) else ()
    env = entry.get("env")
    env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
    cwd = entry.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    return MCPServerConfig(name=name, command=command, args=args, env=env, cwd=cwd)


def discover_mcp_configs(
    workspace: Path, host_context: HostContext, log: PolicyLog
) -> list[MCPServerConfig]:
    """Discover stdio MCP servers, nearest scope winning on a name collision."""
    if host_context.host is Host.CODEX:
        return _discover_codex_mcp(host_context, log)
    return _discover_claude_mcp(workspace, host_context, log)


def _merge_mcp(scopes: list[dict], log: PolicyLog) -> list[MCPServerConfig]:
    """Merge ordered scopes (nearest first) into a de-duplicated server list."""
    resolved: dict[str, MCPServerConfig] = {}
    for servers in scopes:
        for name, entry in servers.items():
            if str(name) in resolved:
                continue
            config = _parse_mcp_entry(str(name), entry)
            if config is None:
                log(f"MCP server {name!r} is disabled, malformed, or non-stdio; skipped")
                continue
            resolved[str(name)] = config
    return list(resolved.values())


def _discover_claude_mcp(
    workspace: Path, host_context: HostContext, log: PolicyLog
) -> list[MCPServerConfig]:
    user_home = host_context.keys_path.parent.parent
    root = project_root(workspace)
    project_mcp = _read_json(root / ".mcp.json") or {}
    claude_json = _read_json(user_home / ".claude.json") or {}
    project_entry: dict = {}
    projects = claude_json.get("projects")
    if isinstance(projects, dict):
        entry = projects.get(str(root)) or projects.get(str(workspace))
        if isinstance(entry, dict) and isinstance(entry.get("mcpServers"), dict):
            project_entry = entry["mcpServers"]
    user_servers = claude_json.get("mcpServers")
    user_servers = user_servers if isinstance(user_servers, dict) else {}
    project_servers = project_mcp.get("mcpServers")
    project_servers = project_servers if isinstance(project_servers, dict) else {}
    # Nearest scope wins: project .mcp.json > project entry > user scope.
    return _merge_mcp([project_servers, project_entry, user_servers], log)


def _discover_codex_mcp(host_context: HostContext, log: PolicyLog) -> list[MCPServerConfig]:
    config = host_context.keys_path.parent / "config.toml"
    data = _read_toml(config) if config.exists() else None
    servers = (data or {}).get("mcp_servers")
    return _merge_mcp([servers if isinstance(servers, dict) else {}], log)


def select_mcp_configs(
    configs: list[MCPServerConfig], selection: object, log: PolicyLog
) -> list[MCPServerConfig]:
    """Apply the resolved ``mcp`` selection (a name list, or None → all).

    The dispatcher resolves the selector at dispatch time and stores the RESOLVED
    name list, so a resume replays exact names. Here a stored list keeps only the
    named servers (in discovery order); None keeps all.
    """
    if not isinstance(selection, list):
        return configs
    wanted = [str(name) for name in selection]
    by_name = {config.name: config for config in configs}
    for name in wanted:
        if name not in by_name:
            log(f"selected MCP server {name!r} matches no discovered server; skipped")
    return [by_name[name] for name in wanted if name in by_name]


class WorkerMCP:
    """The per-Job MCP client: one background asyncio loop holding every session.

    The official ``mcp`` client is asyncio-based while the loop is synchronous, so
    this owns ONE background thread running a private event loop that holds every
    stdio session; synchronous callers submit via ``run_coroutine_threadsafe``
    futures bounded by the per-call timeout. Servers connect CONCURRENTLY at Job
    start, each individually bounded (turn-1 delay = max, not sum).
    """

    def __init__(self, configs: list[MCPServerConfig], log: PolicyLog) -> None:
        self._configs = configs
        self._log = log
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, object] = {}
        self._shutdown: dict[str, asyncio.Event] = {}
        self._tasks: list[asyncio.Task] = []
        #: The immutable, byte-stable advertised tools list, built once.
        self._schemas: list[dict] = []
        #: full advertised name → (server_name, tool_name).
        self._routes: dict[str, tuple[str, str]] = {}

    def start(self) -> None:
        """Connect every server, snapshot its tool schemas, build the tools array."""
        if not self._configs:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="chinamax-mcp"
        )
        self._thread.start()
        pending: list[tuple[MCPServerConfig, concurrent.futures.Future]] = []
        for config in self._configs:
            future = asyncio.run_coroutine_threadsafe(self._connect(config), self._loop)
            pending.append((config, future))
        collected: list[tuple[str, list]] = []
        for config, future in pending:
            try:
                tools = future.result(timeout=_MCP_CONNECT_TIMEOUT_S)
            except Exception as error:  # noqa: BLE001 - a bad server never fails the Job
                kind = type(error).__name__
                self._log(
                    f"MCP server {config.name!r} failed to start ({kind}); proceeding without it"
                )
                continue
            collected.append((config.name, tools))
        self._build_snapshot(collected)

    def _build_snapshot(self, collected: list[tuple[str, list]]) -> None:
        """Deterministic server & tool ordering → a byte-stable tools array."""
        schemas: list[dict] = []
        for server_name, tools in sorted(collected, key=lambda item: item[0]):
            for tool in sorted(tools, key=lambda one: one.name):
                advertised = f"mcp__{server_name}__{tool.name}"
                if advertised in self._routes:
                    self._log(f"MCP tool name collision on {advertised!r}; later duplicate skipped")
                    continue
                # The SDK exposes the input schema as ``input_schema`` (mcp >=2)
                # or ``inputSchema`` (mcp 1.x); accept either.
                schema = getattr(tool, "input_schema", None)
                if schema is None:
                    schema = getattr(tool, "inputSchema", None)
                if not isinstance(schema, dict):
                    schema = {"type": "object"}
                schemas.append(
                    {
                        "name": advertised,
                        "description": tool.description or "",
                        "input_schema": schema,
                    }
                )
                self._routes[advertised] = (server_name, tool.name)
        self._schemas = schemas

    async def _connect(self, config: MCPServerConfig) -> list:
        """Create the server's serve task and await its readiness."""
        ready: asyncio.Future = self._loop.create_future()
        task = self._loop.create_task(self._serve(config, ready))
        self._tasks.append(task)
        return await ready

    async def _serve(self, config: MCPServerConfig, ready: asyncio.Future) -> None:
        """Hold one stdio session open until shutdown; enter/exit in one task."""
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        try:
            params = StdioServerParameters(
                command=config.command,
                args=list(config.args),
                env={**os.environ, **config.env} if config.env else None,
                cwd=config.cwd,
            )
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                event = asyncio.Event()
                self._sessions[config.name] = session
                self._shutdown[config.name] = event
                if not ready.done():
                    ready.set_result(list(listed.tools))
                await event.wait()
        except Exception as error:  # noqa: BLE001 - reported to the caller as a start failure
            if not ready.done():
                ready.set_exception(error)

    @property
    def schemas(self) -> list[dict]:
        """The immutable advertised MCP tool schemas (same object every turn)."""
        return self._schemas

    def is_mcp_tool(self, name: str) -> bool:
        """Report whether an advertised name routes to an MCP server (exact lookup)."""
        return name in self._routes

    def dispatch(self, name: str, value: object) -> tuple[str, bool]:
        """Route an advertised MCP name to its owning server and normalize the result.

        Never raises: a dead/silent server, a bad name, or a timeout yields an
        error-flavored observation, and the SAME output bound native dispatch uses.
        """
        route = self._routes.get(name)
        if route is None or self._loop is None:
            return f"unknown MCP tool {name!r}", True
        server_name, tool_name = route
        session = self._sessions.get(server_name)
        if session is None:
            return f"MCP server {server_name!r} is not connected", True
        arguments = value if isinstance(value, dict) else {}
        try:
            future = asyncio.run_coroutine_threadsafe(
                session.call_tool(tool_name, arguments), self._loop
            )
            result = future.result(timeout=_MCP_CALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            return f"MCP tool {name!r} timed out after {_MCP_CALL_TIMEOUT_S}s", True
        except Exception as error:  # noqa: BLE001 - never let an MCP fault kill the Job
            return f"MCP tool {name!r} failed: {type(error).__name__}: {error}", True
        return _normalize_mcp_result(result)

    def close(self) -> None:
        """Tear every session down, bounded; a hung close falls through to tree-kill."""
        loop = self._loop
        if loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown_all(), loop)
            future.result(timeout=_MCP_CLOSE_TIMEOUT_S)
        except Exception as error:  # noqa: BLE001 - teardown is best-effort and bounded
            self._log(f"MCP teardown did not complete cleanly ({type(error).__name__})")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=_MCP_CLOSE_TIMEOUT_S)
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass
            self._loop = None

    async def _shutdown_all(self) -> None:
        for event in self._shutdown.values():
            event.set()
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=_MCP_CLOSE_TIMEOUT_S)


def _normalize_mcp_result(result: object) -> tuple[str, bool]:
    """Concatenate an MCP result's text blocks; non-text renders as a placeholder.

    ``isError`` → error-flavored tool_result; the SAME output truncation bound
    native dispatch uses (``truncate_tail``) applies, so routing ahead of the
    Registry does not bypass its size guarantee.
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(getattr(block, "text", ""))
        elif kind == "image":
            parts.append(f"[image content: {getattr(block, 'mimeType', 'unknown')}]")
        elif kind in ("resource", "resource_link"):
            parts.append("[embedded resource content]")
        else:
            parts.append(f"[{kind or 'non-text'} content]")
    text = "\n".join(parts) if parts else "(the MCP tool returned no content)"
    return truncate_tail(text), bool(getattr(result, "is_error", False))


# ════════════════════════════════════════════════════════════════════════════
# The Policy façade the loop drives
# ════════════════════════════════════════════════════════════════════════════


class Policy:
    """One Job's discovered Host policy: hooks, Memory, and MCP configs.

    Discovery is done ONCE at construction (no subprocess, no server spawn).
    MCP servers spawn on :meth:`start_mcp` (inside the loop's ``try``); hooks run
    per tool call; Memory blocks are assembled on demand. Every method degrades
    fail-open and NEVER raises.
    """

    def __init__(
        self,
        workspace: Path,
        host_context: HostContext,
        hooks: HookConfig,
        memory: list[MemoryFile],
        mcp_configs: list[MCPServerConfig],
        transcript_path: str,
        job_id: str,
        log: PolicyLog,
        memory_enabled: bool = False,
    ) -> None:
        self.workspace = workspace
        self.host_context = host_context
        self._hooks = hooks
        self._memory = memory
        self._mcp_configs = mcp_configs
        self._transcript_path = transcript_path
        self._job_id = job_id
        self._log = log
        self._mcp: WorkerMCP | None = None
        #: The pinned Memory toggle. LOAD-BEARING: lazy nested-Memory injection
        #: discovers files on demand and never reads ``self._memory``, so
        #: ``lazy_blocks_for_path`` short-circuits on this when memory is OFF.
        self._memory_enabled = memory_enabled

    @classmethod
    def build(
        cls,
        spec,
        reporter: Callable[[str, str], None] | None = None,
        *,
        host_context: HostContext | None = None,
    ) -> "Policy":
        """Discover a Job's Host policy, resolving paths from the spec's Host."""
        log = _make_log(reporter)
        workspace = Path(os.path.realpath(spec.workspace))
        if host_context is None:
            host_context = _resolve_host_context(getattr(spec, "host", None))
        # Each disabled feature SKIPS its discovery entirely (not discover-then-
        # drop) and substitutes the empty value (ADR 0016 amended 0.7.0).
        if spec.hooks_enabled:
            hooks = _safely(
                lambda: discover_hooks(workspace, host_context, log),
                HookConfig(False, (), (), ()),
                log,
                "hook discovery",
            )
        else:
            # Toggle OFF is NOT ``disableAllHooks``: ``disabled`` stays False and
            # the EMPTY specs are what suppress firing (the firing logic keys on
            # empty specs, never on ``disabled``). Do not "fix" this to True.
            hooks = HookConfig(False, (), (), ())
        if spec.memory_enabled:
            memory = _safely(
                lambda: discover_memory(workspace, host_context, log),
                [],
                log,
                "memory discovery",
            )
        else:
            memory = []
        # MCP's disabled signal is the pinned list itself: only a non-empty
        # concrete pin discovers and connects. ``[]``/None (OFF, or a legacy
        # record coerced to []) skips discovery. An ON dispatch that discovered
        # zero servers also skips here — behaviorally identical, accepted.
        if spec.mcp:
            configs = _safely(
                lambda: discover_mcp_configs(workspace, host_context, log),
                [],
                log,
                "MCP discovery",
            )
            configs = select_mcp_configs(configs, spec.mcp, log)
        else:
            configs = []
        return cls(
            workspace=workspace,
            host_context=host_context,
            hooks=hooks,
            memory=memory,
            mcp_configs=configs,
            transcript_path=str(spec.transcript_path),
            job_id=spec.job_id or "",
            log=log,
            memory_enabled=bool(spec.memory_enabled),
        )

    # ── hooks ────────────────────────────────────────────────────────────────

    def _hook_env(self) -> dict:
        env = os.environ.copy()
        if self.host_context.host is Host.CLAUDE:
            env["CLAUDE_PROJECT_DIR"] = str(project_root(self.workspace))
        return env

    def _base_payload(self, event: str) -> dict:
        return {
            "session_id": self._job_id,
            "transcript_path": self._transcript_path,
            "cwd": str(self.workspace),
            "hook_event_name": event,
        }

    def pre_tool_use(self, tool_name: str, tool_input: dict) -> PreResult:
        """Fire PreToolUse hooks; an unexpected raise fails OPEN (allow)."""
        return _safely(
            lambda: self._pre_tool_use(tool_name, tool_input),
            PreResult(True),
            self._log,
            "PreToolUse",
        )

    def _pre_tool_use(self, tool_name: str, tool_input: dict) -> PreResult:
        """Fire PreToolUse hooks for one (already-translated) tool call."""
        specs = self._hooks.for_event("PreToolUse")
        if not specs:
            return PreResult(True)
        payload = {
            **self._base_payload("PreToolUse"),
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
        contexts: list[str] = []
        env = self._hook_env()
        for spec in specs:
            if not _matches(spec, tool_name, self._log):
                continue
            outcome = self._decide_pre(spec, payload, env, tool_name)
            if not outcome.allowed:
                return outcome
            contexts.extend(outcome.contexts)
        return PreResult(True, contexts=tuple(contexts))

    def _decide_pre(self, spec: HookSpec, payload: dict, env: dict, tool_name: str) -> PreResult:
        output = _run_hook(spec, payload, self.workspace, env, self._log)
        if output.spawn_error is not None:
            self._log(f"PreToolUse hook could not run ({output.spawn_error}); continuing (allow)")
            return PreResult(True)
        if output.timed_out:
            self._log(f"PreToolUse hook timed out on {tool_name}; continuing (allow)")
            return PreResult(True)
        if output.exit_code == 2:
            reason = output.stderr.strip() or "a PreToolUse hook denied this tool call (exit 2)"
            self._log(f"PreToolUse hook denied {tool_name}: {reason}")
            return PreResult(False, reason=reason)
        if output.exit_code != 0:
            self._log(
                f"PreToolUse hook crashed (exit {output.exit_code}) on {tool_name}; continuing (allow)"
            )
            return PreResult(True)
        data = _decode_hook_stdout(output.stdout)
        if data is None:
            return PreResult(True)
        return self._parse_pre_json(data, tool_name)

    def _parse_pre_json(self, data: dict, tool_name: str) -> PreResult:
        contexts: list[str] = []
        extra = data.get("hookSpecificOutput")
        decision = extra.get("permissionDecision") if isinstance(extra, dict) else None
        reason = extra.get("permissionDecisionReason") if isinstance(extra, dict) else None
        legacy = data.get("decision")
        context = _hook_context(data)
        if context is not None:
            contexts.append(context)
        if decision == "deny" or legacy == "block":
            text = (
                (reason if isinstance(reason, str) and reason else None)
                or (data.get("reason") if isinstance(data.get("reason"), str) else None)
                or "a PreToolUse hook denied this tool call"
            )
            self._log(f"PreToolUse hook denied {tool_name}: {text}")
            return PreResult(False, reason=text)
        if decision == "ask":
            note = reason if isinstance(reason, str) and reason else "(no reason given)"
            self._log(f"PreToolUse hook returned ask on {tool_name} → allowed; reason: {note}")
        return PreResult(True, contexts=tuple(contexts))

    def post_tool_use(
        self, tool_name: str, tool_input: dict, content: str, is_error: bool
    ) -> list[str]:
        """Fire PostToolUse hooks; an unexpected raise fails OPEN (no context)."""
        return _safely(
            lambda: self._post_tool_use(tool_name, tool_input, content, is_error),
            [],
            self._log,
            "PostToolUse",
        )

    def _post_tool_use(
        self, tool_name: str, tool_input: dict, content: str, is_error: bool
    ) -> list[str]:
        """Fire PostToolUse hooks (successful dispatches only) and gather context."""
        specs = self._hooks.for_event("PostToolUse")
        if not specs or is_error:
            return []
        payload = {
            **self._base_payload("PostToolUse"),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": {"content": content, "is_error": is_error},
        }
        contexts: list[str] = []
        env = self._hook_env()
        for spec in specs:
            if not _matches(spec, tool_name, self._log):
                continue
            contexts.extend(self._collect_post(spec, payload, env, tool_name))
        return contexts

    def _collect_post(self, spec: HookSpec, payload: dict, env: dict, tool_name: str) -> list[str]:
        output = _run_hook(spec, payload, self.workspace, env, self._log)
        if output.spawn_error is not None:
            self._log(f"PostToolUse hook could not run ({output.spawn_error}); continuing")
            return []
        if output.timed_out:
            self._log(f"PostToolUse hook timed out on {tool_name}; continuing")
            return []
        if output.exit_code == 2:
            text = output.stderr.strip()
            return [text] if text else []
        if output.exit_code != 0:
            self._log(
                f"PostToolUse hook crashed (exit {output.exit_code}) on {tool_name}; continuing"
            )
            return []
        data = _decode_hook_stdout(output.stdout)
        if data is None:
            return []
        contexts: list[str] = []
        context = _hook_context(data)
        if context is not None:
            contexts.append(context)
        if data.get("decision") == "block":
            reason = data.get("reason")
            if isinstance(reason, str) and reason.strip():
                contexts.append(reason)
        return contexts

    def stop(self, stop_hook_active: bool) -> StopResult:
        """Fire Stop hooks; an unexpected raise fails OPEN (not blocked)."""
        return _safely(
            lambda: self._stop(stop_hook_active),
            StopResult(False),
            self._log,
            "Stop",
        )

    def _stop(self, stop_hook_active: bool) -> StopResult:
        """Fire Stop hooks on a validating report_result; first block wins."""
        specs = self._hooks.for_event("Stop")
        if not specs:
            return StopResult(False)
        payload = {**self._base_payload("Stop"), "stop_hook_active": stop_hook_active}
        env = self._hook_env()
        for spec in specs:
            if not _matches(spec, "Stop", self._log):
                continue
            output = _run_hook(spec, payload, self.workspace, env, self._log)
            blocked = self._decide_stop(output)
            if blocked is not None:
                self._log(f"Stop hook blocked report_result: {blocked}")
                return StopResult(True, reason=blocked)
        return StopResult(False)

    def _decide_stop(self, output: _HookOutput) -> str | None:
        if output.spawn_error is not None:
            self._log(f"Stop hook could not run ({output.spawn_error}); continuing")
            return None
        if output.timed_out:
            self._log("Stop hook timed out; continuing")
            return None
        if output.exit_code == 2:
            return output.stderr.strip() or "a Stop hook blocked this report_result (exit 2)"
        if output.exit_code not in (0, None):
            self._log(f"Stop hook crashed (exit {output.exit_code}); continuing")
            return None
        data = _decode_hook_stdout(output.stdout)
        if data is None:
            return None
        if data.get("decision") == "block":
            reason = data.get("reason")
            return (
                reason
                if isinstance(reason, str) and reason.strip()
                else "a Stop hook blocked this report_result"
            )
        return None

    # ── apply_patch synthesis ────────────────────────────────────────────────

    def _patch_files(self, patch_text: object) -> list[tuple[str, str]]:
        """Re-slice a patch into (file_path, raw segment) pairs, one per file.

        ``parse_patch`` supplies the file list and validity; the RAW segment is
        re-sliced from the original patch text by file boundary, because
        ``parse_patch``'s structures normalize lines and keep no raw spans. An
        unparseable patch yields no per-file events (zero hooks fire).
        """
        if not isinstance(patch_text, str):
            return []
        try:
            edits = parse_patch(patch_text)
        except Exception:  # noqa: BLE001 - unparseable → no events; dispatch will error
            return []
        segments = _slice_patch_segments(patch_text)
        pairs: list[tuple[str, str]] = []
        for index, edit in enumerate(edits):
            target = edit.new_path if edit.new_path is not None else edit.old_path
            raw = segments[index] if index < len(segments) else patch_text
            pairs.append((target or "(unknown)", raw))
        return pairs

    def pre_tool_use_patch(self, patch_text: object) -> PreResult:
        """Per-file PreToolUse Edit events; an unexpected raise fails OPEN (allow)."""
        return _safely(
            lambda: self._pre_tool_use_patch(patch_text),
            PreResult(True),
            self._log,
            "PreToolUse patch",
        )

    def _pre_tool_use_patch(self, patch_text: object) -> PreResult:
        """One PreToolUse Edit event per file; any single deny vetoes the patch."""
        specs = self._hooks.for_event("PreToolUse")
        if not specs:
            return PreResult(True)
        contexts: list[str] = []
        for file_path, raw in self._patch_files(patch_text):
            result = self.pre_tool_use("Edit", {"file_path": file_path, "patch": raw})
            if not result.allowed:
                return result
            contexts.extend(result.contexts)
        return PreResult(True, contexts=tuple(contexts))

    def post_tool_use_patch(self, patch_text: object) -> list[str]:
        """One PostToolUse Edit event per file after a successful apply."""
        specs = self._hooks.for_event("PostToolUse")
        if not specs:
            return []
        contexts: list[str] = []
        for file_path, raw in self._patch_files(patch_text):
            contexts.extend(
                self.post_tool_use("Edit", {"file_path": file_path, "patch": raw}, "applied", False)
            )
        return contexts

    # ── memory ───────────────────────────────────────────────────────────────

    def derive_injected(self, messages: list[dict]) -> set[str]:
        """Seed the already-injected Memory set ONCE from the replayed transcript."""
        return _safely(
            lambda: scan_injected_paths(messages), set(), self._log, "injected-set scan"
        )

    def first_turn_memory_block(self, injected: set[str]) -> str | None:
        """Assemble the fresh-Job Memory block; an unexpected raise fails OPEN (None)."""
        return _safely(
            lambda: self._first_turn_memory_block(injected),
            None,
            self._log,
            "memory injection",
        )

    def _first_turn_memory_block(self, injected: set[str]) -> str | None:
        """Assemble the fresh-Job Memory block, recording its paths in ``injected``."""
        fresh = [memory for memory in self._memory if memory.path not in injected]
        if not fresh:
            return None
        for memory in fresh:
            injected.add(memory.path)
        return build_memory_block(fresh)

    def lazy_blocks_for_path(self, name: str, value: object, injected: set[str]) -> list[str]:
        """Assemble lazy Memory blocks for a touched subdirectory's Memory files.

        LOAD-BEARING short-circuit for memory OFF: ``_lazy_blocks`` discovers
        Memory files on demand and never reads ``self._memory``, so without this
        guard a memory-OFF Job would still lazy-inject on first touch.
        """
        if not self._memory_enabled:
            return []
        return _safely(
            lambda: self._lazy_blocks(name, value, injected), [], self._log, "lazy memory"
        )

    def _lazy_paths(self, name: str, value: object) -> list[str]:
        if name == "apply_patch":
            patch_text = value.get("patch") if isinstance(value, dict) else None
            return [file_path for file_path, _ in self._patch_files(patch_text)]
        if name in _PATH_TOOLS and isinstance(value, dict):
            path = value.get("path")
            return [path] if isinstance(path, str) and path else []
        return []

    def _lazy_blocks(self, name: str, value: object, injected: set[str]) -> list[str]:
        directories: list[Path] = []
        for raw in self._lazy_paths(name, value):
            try:
                resolved = resolve_in_workspace(self.workspace, raw)
            except Exception:  # noqa: BLE001 - never raise from the policy layer
                continue
            base = resolved if resolved.is_dir() else resolved.parent
            for directory in [base, *base.parents]:
                if directory == self.workspace:
                    break
                if not directory.is_relative_to(self.workspace):
                    break
                if directory not in directories:
                    directories.append(directory)
        blocks: list[str] = []
        names = _memory_filenames(self.host_context)
        # Shallow-to-deep so a nested chain injects in ancestor order.
        for directory in sorted(set(directories), key=lambda d: len(d.parts)):
            fresh: list[MemoryFile] = []
            seen: set[str] = set(injected)
            for basename in names:
                _collect_memory_file(
                    directory / basename, self.host_context, seen, fresh, self._log, 0
                )
            fresh = [memory for memory in fresh if memory.path not in injected]
            if not fresh:
                continue
            for memory in fresh:
                injected.add(memory.path)
            blocks.append(build_memory_block(fresh))
        return blocks

    # ── MCP ──────────────────────────────────────────────────────────────────

    def start_mcp(self) -> None:
        """Spawn every selected MCP server and snapshot its tools (bounded)."""
        if not self._mcp_configs:
            return
        self._mcp = WorkerMCP(self._mcp_configs, self._log)
        try:
            self._mcp.start()
        except Exception as error:  # noqa: BLE001 - a startup failure never fails the Job
            self._log(f"MCP startup failed ({type(error).__name__}); proceeding without MCP")
            self._mcp = None

    def mcp_schemas(self) -> list[dict]:
        return self._mcp.schemas if self._mcp is not None else []

    def is_mcp_tool(self, name: str) -> bool:
        return self._mcp is not None and self._mcp.is_mcp_tool(name)

    def dispatch_mcp(self, name: str, value: object) -> tuple[str, bool]:
        if self._mcp is None:
            return f"unknown MCP tool {name!r}", True
        return self._mcp.dispatch(name, value)

    def close(self) -> None:
        """Tear down MCP servers; the loop calls this in its ``finally``."""
        if self._mcp is not None:
            try:
                self._mcp.close()
            except Exception as error:  # noqa: BLE001 - teardown is best-effort
                self._log(f"MCP close raised ({type(error).__name__}); ignored")
            self._mcp = None


def _slice_patch_segments(patch_text: str) -> list[str]:
    """Split a patch's raw text into one segment per ``--- `` file header."""
    lines = patch_text.split("\n")
    boundaries = [index for index, line in enumerate(lines) if line.startswith("--- ")]
    segments: list[str] = []
    for position, start in enumerate(boundaries):
        end = boundaries[position + 1] if position + 1 < len(boundaries) else len(lines)
        segments.append("\n".join(lines[start:end]))
    return segments


def _resolve_host_context(host_value: object) -> HostContext:
    """Resolve the HostContext for the spec's Host without re-resolving WHICH Host."""
    from chinamax import host as host_module

    if isinstance(host_value, str) and host_value:
        try:
            return HostContext.from_host(Host(host_value))
        except ValueError:
            pass
    return host_module.current_host()


def _make_log(reporter: Callable[[str, str], None] | None) -> PolicyLog:
    """Build the policy logger: the reporter mirror, or the warning channel.

    On the record-less ``exec`` path the reporter silently swallows, so
    ``[policy]`` lines fall back to the structured ``emit_event`` warning channel.
    """
    if reporter is None:

        def log(message: str) -> None:
            emit_event("warning", {"message": POLICY_LOG_PREFIX + message})

        return log

    from chinamax.loop import PHASE_RUNNING_TOOL

    def log(message: str) -> None:
        try:
            reporter(PHASE_RUNNING_TOOL, POLICY_LOG_PREFIX + message)
        except Exception:  # noqa: BLE001 - an observability failure is never fatal
            emit_event("warning", {"message": POLICY_LOG_PREFIX + message})

    return log


def _safely(action: Callable, default, log: PolicyLog, label: str):
    """Run a discovery action fail-open: log and return ``default`` on any error."""
    try:
        return action()
    except Exception as error:  # noqa: BLE001 - the policy layer never raises
        log(f"{label} failed ({type(error).__name__}: {error}); continuing without it")
        return default
