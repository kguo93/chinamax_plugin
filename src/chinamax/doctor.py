"""The `/chinamax:setup` environment doctor.

One diagnosis pass covers everything a first run needs: the ``chinamax`` conda env,
the three dependencies importable BY THE RESOLVED ENV PYTHON (never the interpreter
the doctor itself runs under — a bootstrap run on a fresh machine has ``chinamax``
on its own ``PYTHONPATH`` and would otherwise grade itself), the API-key entries
per Profile (present/missing by NAME — values never touch any stream), and the
state root's writability (both the root and the per-workspace dir it grades,
since jobs/01 shifts the root between ``$CLAUDE_PLUGIN_DATA/state`` and the XDG
fallback). It also records the resolved env python where the Bridge and the shims
read it first.

When a Platform Prerequisite (bash, Git for Windows, Miniconda) is missing, setup
PAUSES before any fix: `diagnose` emits per-tool Rectification commands and
`run_setup` exits without installing a Prerequisite or running a fixer. The Host
agent installs them only after the operator approves; Python never installs a
Prerequisite. `diagnose` still ran first, so its idempotent state-probe and
interpreter record happen even on the paused path.

The env-location and import probes are injectable so the suite can drive the
doctor hermetically without resolving this machine's real conda env (ADR 0011).

With every Prerequisite present, the doctor also FIXES what it can (2026-07-27):
an absent env is created (``conda create``), missing deps are installed
(``pip install -e '<repo>[test]'`` under the env python), and a missing
``~/.claude/model-keys.env`` is scaffolded as a commented template the operator
fills in. The fixers are injectable like the probes; a healthy machine's setup
runs no fixer and stays a pure report. Extending the plugin to any provider that
implements the Anthropic-compatible Messages API is an overlay row plus a key
line — see ``key_template_text``.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import ntpath
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from chinamax import ChinamaxError, profiles, state
from chinamax.host import Host, HostContext, current_host

#: The three dependencies the doctor grades, in report order. ``pytest`` is in
#: runtime/01's optional ``[test]`` extra, which is why the create advice below
#: installs with that extra: the doctor's own advice must satisfy its own check.
DEPS = ("chinamax", "anthropic", "pytest", "tomlkit", "mcp")

#: The env the plugin's Runtime runs in, and the interpreter the shims prefer.
ENV_NAME = "chinamax"
#: The single-line record the shims / Bridge read the resolved interpreter from.
PYTHON_PATH_FILENAME = "python-path"

#: Injectable probe types: resolve the env's absolute python (None when absent),
#: and report which of DEPS import under a given python.
EnvPythonFinder = Callable[[], "str | None"]
DepChecker = Callable[[str], "dict[str, bool]"]

#: Injectable fixer types: create the env, and install the deps under the
#: resolved env python. Each returns ``(ok, detail)``; a detail is a bounded
#: output tail or advice line and never carries a key value.
EnvCreator = Callable[[], "tuple[bool, str]"]
DepInstaller = Callable[[str], "tuple[bool, str]"]

#: Ceiling on each fix subprocess (conda create / pip install). Generous —
#: solver and wheel builds are slow — but bounded, so setup can never hang.
FIX_TIMEOUT_S = 900
PLUGIN_VERSION = "0.7.1"
MANAGED_AGENT_MARKER = "# chinamax-managed-plugin-version:"

#: The three per-Host Policy toggles setup can write (ADR 0016 amended 0.7.0).
_POLICY_TOGGLE_KEYS = ("memory", "hooks", "mcp")


def _parse_policy_toggle_args(tokens: list[str]) -> dict[str, bool]:
    """Parse ``memory=on hooks=off mcp=on`` tokens into a validated toggle subset.

    Any subset is allowed and there is no prompting; an unknown key or a value
    other than ``on``/``off`` is a usage error.

    Args:
        tokens: The positional ``setup`` toggle tokens (possibly empty).

    Returns:
        The named toggles to write (empty when no tokens were supplied).

    Raises:
        ChinamaxError: On an unknown toggle key or a non-``on|off`` value.
    """
    updates: dict[str, bool] = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep or key not in _POLICY_TOGGLE_KEYS:
            raise ChinamaxError(
                f"setup toggle {token!r} must be one of memory=on|off, "
                "hooks=on|off, mcp=on|off"
            )
        normalized = value.strip().lower()
        if normalized not in ("on", "off"):
            raise ChinamaxError(
                f"setup toggle {key!r} must be 'on' or 'off', not {value!r}"
            )
        updates[key] = normalized == "on"
    return updates

# ── Git for Windows install tree ────────────────────────────────────────────
# Git for Windows scatters these across its tree and, with the recommended
# installer option, leaves bash/cygpath OFF PATH — so setup probes the install
# roots on disk. Roots: system-wide %ProgramFiles%\Git, per-user
# %LocalAppData%\Programs\Git.
_GIT_FOR_WINDOWS_URL = "https://git-scm.com/download/win"

# The tool table lives in `state` as the single source the Prerequisite probe
# below AND the Runtime bash spawn both resolve through (ADR 0015); aliased here
# so the setup report keeps its module-qualified name.
_GIT_FOR_WINDOWS_EXES = state.GIT_FOR_WINDOWS_EXES

# ── Miniconda Rectification source ──────────────────────────────────────────
# `latest` over HTTPS, no version pin and no checksum (ADR 0009, operator's
# explicit decision). One installer filename per (Platform, normalized arch).
_MINICONDA_URL_BASE = "https://repo.anaconda.com/miniconda/"
# platform.machine() normalization: any value absent here is an unsupported
# architecture and yields an advice-only miniconda row (never a 404-bound URL).
_LINUX_MINICONDA_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
_DARWIN_MINICONDA_ARCH = {"arm64": "arm64", "x86_64": "x86_64"}

# ── Linux bash Rectification ────────────────────────────────────────────────
# First package manager on PATH wins; each row installs bash and runs only when
# passwordless sudo works (the skill's `sudo -n true` gate), else operator-run.
_LINUX_BASH_PACKAGE_MANAGERS = (
    ("apt-get", "sudo apt-get install -y bash"),
    ("dnf", "sudo dnf install -y bash"),
    ("yum", "sudo yum install -y bash"),
    ("pacman", "sudo pacman -S --noconfirm bash"),
    ("zypper", "sudo zypper install -y bash"),
    ("apk", "sudo apk add bash"),
)


def required_deps() -> tuple[str, ...]:
    """Return the import requirements for the active operating system."""
    if sys.platform == "win32":
        return (*DEPS, "psutil", "filelock")
    if sys.platform == "darwin":
        return (*DEPS, "psutil")
    return DEPS


def _find_conda() -> str | None:
    """Absolute conda launcher, ~/miniconda3 first, PATH fallback; None when absent.

    The Miniconda Prerequisite is "conda resolvable": an existing anaconda/miniforge
    conda on PATH counts as present, so setup never forces its own install location
    on a machine that already has conda. Not memoized — the suite monkeypatches
    ``Path.home``/``sys.platform``/``shutil.which`` and a cache would leak stale
    resolutions across tests.
    """
    base = Path.home() / "miniconda3"
    if sys.platform == "win32":
        candidates = [base / "Scripts" / "conda.exe", base / "condabin" / "conda.bat"]
    else:
        candidates = [base / "bin" / "conda"]
    for candidate in candidates:
        if _is_executable(str(candidate)):
            return str(candidate)
    return shutil.which("conda")


def prerequisite_status() -> dict[str, bool]:
    """External Prerequisite tools setup requires, by Platform.

    Linux and macOS: ``bash`` (on PATH) and ``miniconda`` (conda resolvable).
    Windows: ``git``/``bash``/``cygpath`` (Git for Windows install tree first, then
    PATH) followed by ``miniconda``; the Git row must precede miniconda because the
    miniconda row's ``conda init ... bash`` needs the bash the Git row provides.
    Miniconda is "conda resolvable" everywhere (``~/miniconda3`` first, then PATH).
    Off-matrix Platforms return ``{}`` (ADR 0015 target matrix only).
    """
    if sys.platform == "win32":
        status = {name: _windows_tool_present(name) for name in _GIT_FOR_WINDOWS_EXES}
        status["miniconda"] = _find_conda() is not None
        return status
    if sys.platform == "darwin":
        return {"bash": shutil.which("bash") is not None, "miniconda": _find_conda() is not None}
    if sys.platform.startswith("linux"):
        return {"bash": shutil.which("bash") is not None, "miniconda": _find_conda() is not None}
    return {}


def _windows_tool_present(name: str) -> bool:
    """True when a tool resolves in a Git for Windows install root, else on PATH."""
    return state.windows_tool_path(name) is not None


def prerequisite_fixes(prerequisites: dict[str, bool]) -> list[dict]:
    """Rows for each missing Prerequisite: name, summary, commands, run_policy, shell, install_location.

    Plain dicts (repo convention). ``run_policy`` is ``"agent"`` (the agent runs the
    commands), ``"privileged"`` (run only when ``sudo -n true`` succeeds, else handed
    to the operator), or ``"operator"`` (advice-only, ``commands`` empty). ``shell``
    names the interpreter the commands are written for so the adapter never guesses:
    Windows ``"cmd"`` rows run via ``cmd /c`` and ``"powershell"``/``"native"`` rows
    run natively — neither is ever pasted into Git Bash. ``install_location`` is a
    DISPLAY template (env-var/``$HOME`` forms as written), not a resolved absolute
    path. Emission order is Prerequisite order: on Windows the Git for Windows row
    precedes the miniconda row (whose ``conda init ... bash`` needs the Git bash).
    """
    missing = [name for name, present in prerequisites.items() if not present]
    if not missing:
        return []
    rows: list[dict] = []
    if sys.platform == "win32":
        git_missing = [name for name in ("git", "bash", "cygpath") if name in missing]
        if git_missing:
            rows.append(_windows_git_row(git_missing))
        if "miniconda" in missing:
            rows.append(_windows_miniconda_row())
        return rows
    if sys.platform == "darwin":
        if "bash" in missing:
            rows.append(_darwin_bash_row())
        if "miniconda" in missing:
            rows.append(_miniconda_posix_row("darwin"))
        return rows
    if sys.platform.startswith("linux"):
        if "bash" in missing:
            rows.append(_linux_bash_row())
        if "miniconda" in missing:
            rows.append(_miniconda_posix_row("linux"))
        return rows
    return rows


def _linux_bash_row() -> dict:
    """The linux bash Rectification row: first package manager on PATH, else advice."""
    for manager, command in _LINUX_BASH_PACKAGE_MANAGERS:
        if shutil.which(manager):
            return {
                "name": "bash",
                "summary": (
                    f"install bash with {manager}. The skill runs it automatically only when "
                    "passwordless sudo works (sudo -n true); otherwise run it yourself."
                ),
                "commands": [command],
                "run_policy": "privileged",
                "shell": "bash",
                "install_location": "system package manager",
            }
    return {
        "name": "bash",
        "summary": "install bash with your distribution's package manager",
        "commands": [],
        "run_policy": "operator",
        "shell": "bash",
        "install_location": "system package manager",
    }


def _darwin_bash_row() -> dict:
    """The macOS bash Rectification row: brew install when brew exists, else advice."""
    if shutil.which("brew"):
        return {
            "name": "bash",
            "summary": "install a current bash with brew install bash",
            "commands": ["brew install bash"],
            "run_policy": "agent",
            "shell": "bash",
            "install_location": "Homebrew",
        }
    return {
        "name": "bash",
        "summary": "install Homebrew or the Xcode Command Line Tools, then brew install bash",
        "commands": [],
        "run_policy": "operator",
        "shell": "bash",
        "install_location": "Homebrew",
    }


# The winget-absent Windows Git fallback: fail-loud so stop-on-first-failure can
# catch an installer failure. `$ErrorActionPreference='Stop'` aborts on a download
# error, and `Start-Process -Wait -PassThru` + `exit $p.ExitCode` propagates the
# installer's own exit code (`Start-Process -Wait` alone does not). One raw string:
# the line carries both `"` and `'` plus native `\` path separators.
_WINDOWS_GIT_POWERSHELL_FALLBACK = r'''powershell -NoProfile -Command "$ErrorActionPreference='Stop';$a=(Invoke-RestMethod https://api.github.com/repos/git-for-windows/git/releases/latest).assets|Where-Object name -like '*64-bit.exe'|Select-Object -First 1;Invoke-WebRequest $a.browser_download_url -OutFile $env:TEMP\chinamax-git.exe;$p=Start-Process -Wait -PassThru $env:TEMP\chinamax-git.exe -ArgumentList '/VERYSILENT','/NORESTART','/CURRENTUSER';exit $p.ExitCode"'''


def _windows_git_row(missing_tools: list[str]) -> dict:
    """The single deduped Git for Windows Rectification row (the git/bash/cygpath trio).

    winget present → one ``winget install --id Git.Git`` line (one UAC click); winget
    absent → the fail-loud per-user PowerShell fallback, whose summary names
    ``https://git-scm.com/download/win`` as the manual alternative (GitHub API
    rate-limiting can break the automated line). Both run natively, never Git Bash.
    """
    if shutil.which("winget"):
        return {
            "name": "Git for Windows",
            "missing_tools": missing_tools,
            "summary": (
                "install Git for Windows with winget (one UAC consent click); provides "
                "git, bash, and cygpath. Run natively, not in Git Bash."
            ),
            "commands": [
                "winget install --id Git.Git -e --silent "
                "--accept-source-agreements --accept-package-agreements"
            ],
            "run_policy": "agent",
            "shell": "native",
            "install_location": r"Program Files\Git",
        }
    return {
        "name": "Git for Windows",
        "missing_tools": missing_tools,
        "summary": (
            "install Git for Windows per-user via the PowerShell fallback (winget "
            f"absent); if the GitHub API is rate-limited or blocked, install manually "
            f"from {_GIT_FOR_WINDOWS_URL}. Provides git, bash, and cygpath. Run "
            "natively, not in Git Bash."
        ),
        "commands": [_WINDOWS_GIT_POWERSHELL_FALLBACK],
        "run_policy": "agent",
        "shell": "powershell",
        "install_location": r"%LocalAppData%\Programs\Git",
    }


def _windows_miniconda_row() -> dict:
    """The Windows miniconda Rectification row (cmd.exe syntax; run via ``cmd /c``).

    The JustMe installer reuses an existing per-user install, so a re-run is safe.
    Known live-Windows risk (mocked-only, ADR 0015): ``start /wait`` does not
    reliably propagate the installer's exit code to ``%ERRORLEVEL%`` on all Windows
    versions, so stop-on-first-failure may not catch a failed silent install.
    """
    commands = [
        r'curl.exe -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe -o "%TEMP%\chinamax-miniconda.exe"',
        r'start /wait "" "%TEMP%\chinamax-miniconda.exe" /InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /S /D=%USERPROFILE%\miniconda3',
        r'"%USERPROFILE%\miniconda3\Scripts\conda.exe" init cmd.exe powershell bash',
    ]
    return {
        "name": "miniconda",
        "summary": (
            "install Miniconda into %USERPROFILE%\\miniconda3 with curl.exe (cmd.exe "
            "syntax; run each line via cmd /c, never Git Bash). The JustMe installer "
            "reuses an existing per-user install. The final command runs conda init, "
            "which EDITS your cmd/powershell profiles and registry. Requires curl.exe."
        ),
        "commands": commands,
        "run_policy": "agent",
        "shell": "cmd",
        "install_location": r"%USERPROFILE%\miniconda3",
    }


def _miniconda_posix_row(platform_kind: str) -> dict:
    """The linux/macOS miniconda Rectification row (bash syntax), or advice on an
    unsupported architecture.

    ``-b -u`` reuses an existing/partial ``$HOME/miniconda3`` so a re-run after an
    interrupted install is idempotent. The final command runs conda init, which
    MODIFIES the operator's shell startup files. curl is the row's dependency. An
    architecture absent from the normalization map yields an advice-only row with an
    empty ``commands`` and no download URL (never a 404-bound URL).
    """
    machine = platform.machine().lower()
    if platform_kind == "darwin":
        arch = _DARWIN_MINICONDA_ARCH.get(machine)
        os_tag, init_shells, startup_files = "MacOSX", "bash zsh", "~/.bashrc and ~/.zshrc"
    else:
        arch = _LINUX_MINICONDA_ARCH.get(machine)
        os_tag, init_shells, startup_files = "Linux", "bash", "~/.bashrc"
    if arch is None:
        return {
            "name": "miniconda",
            "summary": (
                f"unsupported CPU architecture {platform.machine()!r}; install Miniconda "
                f"manually from {_MINICONDA_URL_BASE}, then re-run setup"
            ),
            "commands": [],
            "run_policy": "operator",
            "shell": "bash",
            "install_location": "$HOME/miniconda3",
        }
    installer = f"Miniconda3-latest-{os_tag}-{arch}.sh"
    url = f"{_MINICONDA_URL_BASE}{installer}"
    commands = [
        f'curl -fsSL {url} -o "$HOME/.chinamax-miniconda.sh"',
        'bash "$HOME/.chinamax-miniconda.sh" -b -u -p "$HOME/miniconda3"',
        f'"$HOME/miniconda3/bin/conda" init {init_shells}',
    ]
    return {
        "name": "miniconda",
        "summary": (
            f"install Miniconda ({installer}) into $HOME/miniconda3 with curl; -b -u "
            "reuses an existing $HOME/miniconda3 so a re-run is idempotent. The final "
            f"command runs conda init {init_shells}, which MODIFIES your shell startup "
            f"files ({startup_files}). Requires curl."
        ),
        "commands": commands,
        "run_policy": "agent",
        "shell": "bash",
        "install_location": "$HOME/miniconda3",
    }


def missing_prerequisite_advice(rows: list[dict]) -> str:
    """One-line 'what is missing and how to install it', or '' when nothing is missing.

    Consumes the pre-computed `prerequisite_fixes` rows (never re-probes the
    filesystem). Names the missing tools (the Windows Git row expands its
    ``missing_tools``) and joins each row's ``summary``, deduped. Shared by both
    Codex message sites so every Host surfaces the same guidance.
    """
    if not rows:
        return ""
    names: list[str] = []
    for row in rows:
        names.extend(row.get("missing_tools") or [row["name"]])
    steps: list[str] = []
    for row in rows:
        if row["summary"] not in steps:
            steps.append(row["summary"])
    return f"missing prerequisites ({', '.join(names)}): " + "; ".join(steps)


def data_root(context: HostContext | None = None) -> Path:
    """Return the plugin data root the interpreter record lives under.

    jobs/01's state-root rule MINUS the ``/state`` suffix: ``$CLAUDE_PLUGIN_DATA``
    when set (no ``/state``), else ``$XDG_STATE_HOME/chinamax`` (the XDG fallback,
    which equals the state root there), else ``~/.local/state/chinamax``. An empty
    or relative value counts as unset, exactly as `state.state_root` treats them.
    """
    return (context or current_host()).data_root


def python_path_file(context: HostContext | None = None) -> Path:
    """Return the path the resolved env python is recorded at."""
    return data_root(context) / PYTHON_PATH_FILENAME


def source_repo_path() -> str:
    """Discover the source repo the create advice should ``pip install -e``.

    The editable install's origin when it can be found — never a hardcoded author
    path, since the fresh machine the doctor exists for keeps the checkout
    somewhere else (PRD user story 14) — else ``${CLAUDE_PLUGIN_ROOT}``.
    """
    try:
        import chinamax

        candidate = Path(chinamax.__file__).resolve().parents[2]
        if (candidate / "pyproject.toml").is_file():
            return str(candidate)
    except (OSError, IndexError, ImportError):
        pass
    root = os.environ.get("PLUGIN_ROOT", "").strip() or os.environ.get(
        "CLAUDE_PLUGIN_ROOT", ""
    ).strip()
    return root or "<the chinamax repo>"


def key_template_text(context: HostContext | None = None) -> str:
    """Render the commented ``model-keys.env`` template from the resolved Profiles.

    Comments only — an untouched template parses to zero keys under
    `profiles.load_keys` — with one commented ``<api_key_env>=`` line per
    RESOLVED Profile (shipped plus overlay), so extending the plugin to any
    Anthropic-compatible provider through the overlay puts its key line in the
    next scaffold. Keep this in lockstep with `profiles.load_profiles`.
    """
    env_names: list[str] = []
    resolved = profiles.load_profiles(context)
    for name in sorted(resolved):
        env_name = resolved[name].api_key_env
        if env_name not in env_names:
            env_names.append(env_name)
    lines = [
        "# chinamax API keys — one KEY=value per line.",
        "# This file is bash-sourceable; values may be quoted:",
        "#   DEEPSEEK_API_KEY='sk-...'",
        "# Lines starting with # and blank lines are ignored.",
        "#",
        "# Extending to more models: any provider that implements the",
        "# Anthropic-compatible Messages API can be added as a Profile. Add a",
        "# row to the selected Host's chinamax-profiles.json — name, base_url (the",
        "# provider's /anthropic endpoint), model, api_key_env, and optionally",
        "# max_tokens and request_extras (extra Messages-request kwargs) — then",
        "# add the matching <api_key_env>=<key> line below. The profiles and",
        "# /chinamax:setup report every resolved Profile's key as PRESENT or",
        "# MISSING by name; key values are never printed.",
        "#",
        "# Uncomment and fill in the profiles you use:",
        "",
    ]
    lines.extend(f"# {env_name}=" for env_name in env_names)
    return "\n".join(lines) + "\n"


def write_key_template(context: HostContext | None = None) -> bool:
    """Scaffold ``~/.claude/model-keys.env`` when absent; never touch an existing file.

    Creation is ``O_EXCL`` at mode 0600, so an existing file — the operator's
    real keys — can never be overwritten, even by two concurrent setups.

    Returns:
        True when the template was created, False when the file already existed.
    """
    path = profiles.keys_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(key_template_text(context))
    return True


def fix_missing(
    report: dict,
    *,
    find_env_python: EnvPythonFinder,
    create_env: EnvCreator,
    install_deps: DepInstaller,
    context: HostContext | None = None,
) -> list[dict]:
    """Fix what the diagnosis found missing; return the fix rows in run order.

    Key-file scaffold first (independent of the env), then the env, then the
    deps under the freshly resolved python. A failed fix is recorded and never
    retried; nothing at all runs on a healthy machine.
    """
    fixes: list[dict] = []
    if write_key_template(context):
        fixes.append(
            {
                "action": "key-template",
                "ok": True,
                "detail": str(profiles.keys_path(context)),
            }
        )
    env_python = report["python"]
    if not report["env"]["present"]:
        ok, detail = create_env()
        fixes.append({"action": "create-env", "ok": ok, "detail": detail})
        if ok:
            env_python = find_env_python()
    if env_python and not all(report["deps"].values()):
        ok, detail = install_deps(env_python)
        fixes.append({"action": "install-deps", "ok": ok, "detail": detail})
    return fixes


def render_fixes(fixes: list[dict]) -> str:
    """Render the fix pass, empty when nothing was fixed. NEVER prints a key value."""
    if not fixes:
        return ""
    lines = ["chinamax setup — fixing"]
    for row in fixes:
        verdict = "ok" if row["ok"] else "FAILED"
        if row["action"] == "key-template":
            lines.append(
                f"  created {row['detail']} — a commented template; fill in your keys"
            )
            continue
        if row["action"] == "create-env":
            lines.append(
                f"  conda env '{ENV_NAME}': conda create -y -n {ENV_NAME} python=3.12  [{verdict}]"
            )
        else:
            lines.append(
                f"  dependencies: pip install -e '<repo>[test]'  [{verdict}]"
            )
        if not row["ok"] and row["detail"]:
            lines.extend(f"      {line}" for line in row["detail"].splitlines())
    return "\n".join(lines) + "\n\n"


def diagnose(
    workspace: str | Path | None = None,
    *,
    find_env_python: EnvPythonFinder | None = None,
    check_deps: DepChecker | None = None,
    record: bool = True,
    context: HostContext | None = None,
) -> dict:
    """Run one full diagnosis and return the pinned ``--json`` document.

    Args:
        workspace: The workspace whose per-workspace state dir is graded.
        find_env_python: Resolve the env's absolute python (None when absent);
            the production resolver by default.
        check_deps: Report which of DEPS import under a python; the production
            subprocess checker by default. Only ever called with a real path —
            when the env is absent every dep is reported missing WITHOUT running
            an import, so a bootstrap run never grades its own interpreter.
        record: Whether to (re-)record the resolved env python.

    Returns:
        The report document matching the pinned schema.
    """
    find_env_python = find_env_python or _find_env_python
    check_deps = check_deps or _check_deps

    env_python = find_env_python()
    env_present = env_python is not None
    names = required_deps()
    deps = check_deps(env_python) if env_present else {name: False for name in names}
    deps = {name: bool(deps.get(name, False)) for name in names}
    prerequisites = prerequisite_status()
    # Compute the Rectification rows ONCE here (they probe the filesystem for pm /
    # winget detection); render_report and missing_prerequisite_advice consume the
    # stored rows rather than re-calling prerequisite_fixes.
    prerequisite_rows = (
        prerequisite_fixes(prerequisites)
        if prerequisites and not all(prerequisites.values())
        else []
    )

    selected = context or current_host()
    root = selected.state_root
    workspace_dir = _workspace_state_dir(root, workspace)
    writable = _state_writable(workspace_dir)

    profile_rows = _profile_rows(selected)
    policy_report = _policy_report(selected)
    ok = (
        env_present
        and all(deps[name] for name in names)
        and writable
        and all(prerequisites.values())
        # A malformed settings.json makes dispatch unusable until it is fixed, so
        # setup must not claim success over it (ADR 0016 amended 0.7.0).
        and not policy_report["malformed"]
    )

    if record and env_present:
        _maybe_record_python(env_python, selected)

    return {
        "ok": ok,
        "python": env_python,
        "state_root": str(root),
        "workspace_state_dir": str(workspace_dir),
        "state_writable": writable,
        "env": {"present": env_present, "path": env_python},
        "deps": {name: deps[name] for name in names},
        "profiles": profile_rows,
        "policy": policy_report,
        **({"prerequisites": prerequisites} if prerequisites else {}),
        **({"prerequisite_fixes": prerequisite_rows} if prerequisite_rows else {}),
    }


def _policy_report(context: HostContext) -> dict:
    """Report the three Policy toggle values, the full settings path, and flags.

    The TOLERANT settings read: a malformed file is a reported flag, never a
    crash. ``present`` distinguishes a fresh (UNSET) file from a written one so
    the Claude setup command knows to prompt.

    Args:
        context: The Host whose ``state_root`` holds the toggle file.

    Returns:
        A dict with ``settings_path`` (full path str), ``present`` (bool),
        ``malformed`` (bool), and the ``memory``/``hooks``/``mcp`` booleans.
    """
    from chinamax import policy

    settings, malformed = policy.inspect_policy_settings(context)
    settings_path = context.state_root / policy.SETTINGS_FILENAME
    return {
        "settings_path": str(settings_path),
        "present": settings_path.exists(),
        "malformed": malformed,
        "memory": settings.memory,
        "hooks": settings.hooks,
        "mcp": settings.mcp,
    }


def render_report(report: dict) -> str:
    """Render the diagnosis as one human-readable pass. NEVER prints a key value."""
    lines = [f"chinamax setup — {'OK' if report['ok'] else 'PROBLEMS FOUND'}"]

    env = report["env"]
    if env["present"]:
        lines.append(f"  conda env '{ENV_NAME}': present ({env['path']})")
    else:
        repo = source_repo_path()
        lines.append(f"  conda env '{ENV_NAME}': MISSING — create it with:")
        lines.append(f"      conda create -y -n {ENV_NAME} python=3.12")
        lines.append(f"      conda run -n {ENV_NAME} pip install -e '{repo}[test]'")

    lines.append(f"  dependencies (imported under the '{ENV_NAME}' env python):")
    for name in report["deps"]:
        lines.append(f"      {name}: {'ok' if report['deps'][name] else 'MISSING'}")

    prerequisites = report.get("prerequisites", {})
    for name, present in prerequisites.items():
        lines.append(f"      prerequisite {name}: {'ok' if present else 'MISSING'}")
    rows = report.get("prerequisite_fixes", [])
    if rows:
        lines.append("  missing prerequisites — rectification commands:")
        for row in rows:
            lines.append(
                f"      {row['name']} [run_policy={row['run_policy']}, shell={row['shell']}, "
                f"install into {row['install_location']}]"
            )
            lines.append(f"          {row['summary']}")
            for command in row["commands"]:
                lines.append(f"          $ {command}")
            if not row["commands"]:
                lines.append("          (install manually — no automatic command)")

    lines.append(f"  API keys ({profiles.keys_path()}):")
    for row in report["profiles"]:
        lines.append(f"      {row['name']}: {row['key']} ({row['key_env']})")

    lines.append("  state directory:")
    lines.append(f"      root: {report['state_root']}")
    lines.append(f"      this workspace: {report['workspace_state_dir']}")
    lines.append(f"      writable: {'yes' if report['state_writable'] else 'NO'}")

    policy = report.get("policy")
    if policy is not None:
        lines.append(f"  policy toggles ({policy['settings_path']}):")
        if policy["malformed"]:
            lines.append("      MALFORMED — dispatch will error until the file is fixed")
        elif not policy["present"]:
            lines.append(
                "      UNSET — choose with: setup memory=<on|off> hooks=<on|off> mcp=<on|off>"
            )
        else:
            for toggle in ("memory", "hooks", "mcp"):
                lines.append(f"      {toggle}: {'on' if policy[toggle] else 'off'}")

    if env["present"]:
        lines.append(f"  interpreter recorded at: {python_path_file()}")
    return "\n".join(lines) + "\n"


def run_setup(
    args: argparse.Namespace,
    *,
    find_env_python: EnvPythonFinder | None = None,
    check_deps: DepChecker | None = None,
    create_env: EnvCreator | None = None,
    install_deps: DepInstaller | None = None,
    permission_mode: str | None = None,
) -> int:
    """Diagnose, fix what is missing, re-diagnose, and report; return the exit code.

    Args:
        args: The parsed ``setup`` arguments (``workspace`` and ``json``).
        find_env_python: Injected env resolver (production default otherwise).
        check_deps: Injected dep checker (production default otherwise).
        create_env: Injected env creator (production ``conda create`` otherwise).
        install_deps: Injected dep installer (production ``pip install -e``
            otherwise).

    Returns:
        0 when everything the plugin needs is in place AFTER the fix pass, 1
        otherwise. Key presence is reported but never fails the run — an unused
        Profile must not block setup.
    """
    find_env_python = find_env_python or _find_env_python
    check_deps = check_deps or _check_deps
    create_env = create_env or _create_env
    install_deps = install_deps or _install_deps

    if current_host().host is Host.CODEX:
        return run_codex_setup(
            args,
            find_env_python=find_env_python,
            check_deps=check_deps,
            create_env=create_env,
            install_deps=install_deps,
            permission_mode=permission_mode,
        )

    # Apply the Policy toggles BEFORE diagnosing so the report reflects the written
    # values. Explicit args write (subset update, no prompting); a no-arg run never
    # writes — a fresh file stays UNSET for the command protocol to prompt on
    # (ADR 0016 amended 0.7.0). A directory at the path raises PolicySettingsError.
    toggle_updates = _parse_policy_toggle_args(getattr(args, "toggles", []) or [])
    if toggle_updates:
        from chinamax import policy

        policy.write_policy_settings(current_host(), toggle_updates)

    report = diagnose(
        args.workspace,
        find_env_python=find_env_python,
        check_deps=check_deps,
    )
    if report.get("prerequisites") and not all(report["prerequisites"].values()):
        report["fixes"] = []
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(render_report(report))
        return 1
    fixes = fix_missing(
        report,
        find_env_python=find_env_python,
        create_env=create_env,
        install_deps=install_deps,
        context=current_host(),
    )
    if any(row["action"] != "key-template" for row in fixes):
        report = diagnose(
            args.workspace,
            find_env_python=find_env_python,
            check_deps=check_deps,
        )
    report["fixes"] = fixes
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_fixes(fixes) + render_report(report))
    return 0 if report["ok"] else 1


def codex_agent_path(context: HostContext | None = None) -> Path:
    """Return the Host-owned native Codex agent target."""
    selected = context or current_host()
    return selected.keys_path.parent / "agents" / "chinamax_bridge.toml"


def _agent_source_path(context: HostContext | None = None) -> Path:
    selected = context or current_host()
    root = selected.plugin_root
    if root is None:
        root = Path(__file__).resolve().parents[2]
    return root / "agents" / "chinamax.md"


def compile_codex_agent(
    source: Path | None = None, *, version: str = PLUGIN_VERSION
) -> str:
    """Compile the shared Markdown agent adapter into deterministic TOML."""
    source = source or _agent_source_path()
    text = source.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not match:
        raise ChinamaxError(f"{source}: missing Markdown frontmatter")
    frontmatter, body = match.groups()
    description = ""
    for line in frontmatter.splitlines():
        if line.startswith("description:"):
            description = line.split(":", 1)[1].strip()
            break
    if not description:
        raise ChinamaxError(f"{source}: missing agent description")
    body = body.strip() + "\n"
    # JSON's escaping is the TOML basic-string escape set for this generated
    # body, and keeps delimiters/newlines from changing the TOML structure.
    return (
        f"{MANAGED_AGENT_MARKER} {version}\n"
        'name = "chinamax_bridge"\n'
        f"description = {json.dumps(description, ensure_ascii=False)}\n"
        'model = "gpt-5.6-terra"\n'
        'model_reasoning_effort = "low"\n'
        f"developer_instructions = {json.dumps(body, ensure_ascii=False)}\n"
    )


def _agent_embedded_version(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline().rstrip("\r\n")
    except OSError:
        return None
    if not first.startswith(MANAGED_AGENT_MARKER):
        return None
    value = first[len(MANAGED_AGENT_MARKER) :].strip()
    return value or None


def _has_managed_agent_marker(path: Path) -> bool:
    """Return whether the native-agent file claims the managed marker."""
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline().startswith(MANAGED_AGENT_MARKER)
    except OSError:
        return False


def _atomic_text_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        state._set_mode(handle.fileno(), mode, fd=True)
    state.atomic_replace(temporary, path)


def sync_managed_agent(
    *, source: Path | None = None, target: Path | None = None, version: str = PLUGIN_VERSION,
    source_name: str = "startup", context: HostContext | None = None
) -> str:
    """Force-sync an existing managed Codex agent after a plugin update."""
    if source_name == "compact":
        return ""
    target = target or codex_agent_path(context)
    if not target.exists():
        return ""
    old = _agent_embedded_version(target)
    if not _has_managed_agent_marker(target):
        return f"Codex native agent {target} is unmanaged or stale; rerun $chinamax-setup."
    if old == version:
        return ""
    try:
        _atomic_text_write(target, compile_codex_agent(source, version=version))
    except Exception as error:  # noqa: BLE001 - SessionStart must continue
        return (
            f"Codex native agent sync failed for {target}: {type(error).__name__}: {error}; "
            "the native agent may be stale; rerun $chinamax-setup."
        )
    old_label = old or "invalid"
    return f"Codex native agent refreshed: {target} ({old_label} -> {version})."


def codex_setup_plan(
    workspace: str | Path | None = None,
    *,
    context: HostContext | None = None,
    find_env_python: EnvPythonFinder | None = None,
    check_deps: DepChecker | None = None,
    policy_toggles: dict[str, bool] | None = None,
) -> dict:
    """Build a redacted, non-mutating Codex setup preview and consent digest."""
    selected = context or current_host()
    find_env_python = find_env_python or _find_env_python
    check_deps = check_deps or _check_deps
    python = find_env_python()
    names = required_deps()
    deps = check_deps(python) if python else {name: False for name in names}
    deps = {name: bool(deps.get(name, False)) for name in names}
    prerequisites = prerequisite_status()
    # The Rectification rows join the DIGESTED structure so consent binds the exact
    # commands: they vary with winget / package-manager detection WITHOUT
    # prerequisites itself changing, so digest placement is load-bearing.
    prerequisite_rows = prerequisite_fixes(prerequisites)
    config_path = selected.keys_path.parent / "config.toml"
    try:
        config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except OSError:
        config_text = ""
    try:
        import tomllib

        parsed = tomllib.loads(config_text) if config_text else {}
    except (OSError, ValueError):
        parsed = {}
    features = parsed.get("features", {}) if isinstance(parsed, dict) else {}
    agents = parsed.get("agents", {}) if isinstance(parsed, dict) else {}
    if not isinstance(features, dict):
        features = {}
    if not isinstance(agents, dict):
        agents = {}
    target = codex_agent_path(selected)
    if not target.exists():
        status = "absent"
    elif _has_managed_agent_marker(target):
        status = (
            "managed-current"
            if _agent_embedded_version(target) == PLUGIN_VERSION
            else "managed-stale"
        )
    else:
        status = "unmanaged-collision"
    proposed = {
        "features.hooks": features.get("hooks") is not True,
        "features.multi_agent": features.get("multi_agent") is not True,
        "agents.enabled": agents.get("enabled") is not True,
    }
    observed = {
        "features.hooks": features.get("hooks"),
        "features.multi_agent": features.get("multi_agent"),
        "agents.enabled": agents.get("enabled"),
    }
    config_diff = _codex_config_diff(config_path, observed, proposed)
    keys_path = profiles.keys_path(selected)
    overlay_path = profiles.overlay_path(selected)
    interpreter_path = python_path_file(selected)
    state_writable = _non_mutating_writable(selected.state_root)
    data_writable = _non_mutating_writable(selected.data_root)
    config_writable = _non_mutating_writable(config_path.parent)
    recorded_python = _read_recorded_python(interpreter_path)
    key_template = {
        "path": str(keys_path),
        "create": not keys_path.exists(),
        "mode": "0600",
    }
    interpreter_change = {
        "path": str(interpreter_path),
        "recorded": recorded_python,
        "update": bool(python and (recorded_python != python or not _is_executable(recorded_python or ""))),
    }
    # The Policy toggles ride the plan and digest: BOTH the observed current
    # settings AND the proposed values are folded in, so a hand-edit between
    # preview and apply changes `observed` and aborts (ADR 0016 amended 0.7.0).
    from chinamax import policy as _policy

    policy_toggles = dict(policy_toggles or {})
    current_settings, policy_malformed = _policy.inspect_policy_settings(selected)
    settings_path = selected.state_root / _policy.SETTINGS_FILENAME
    policy_present = settings_path.exists()
    policy_observed = {
        "present": policy_present,
        "malformed": policy_malformed,
        "memory": current_settings.memory,
        "hooks": current_settings.hooks,
        "mcp": current_settings.mcp,
    }
    # Proposed = what write_policy_settings will resolve to: a well-formed present
    # file preserves its unnamed toggles; an absent/malformed one starts all-OFF;
    # the named toggle args override either way.
    policy_base = (
        {name: getattr(current_settings, name) for name in _POLICY_TOGGLE_KEYS}
        if policy_present and not policy_malformed
        else {name: False for name in _POLICY_TOGGLE_KEYS}
    )
    policy_proposed = {**policy_base, **policy_toggles}
    digest_input = {
        "host": selected.host.value,
        "paths": {
            "keys": str(keys_path),
            "overlay": str(overlay_path),
            "plugin_data": str(selected.data_root),
            "state": str(selected.state_root),
            "interpreter": str(interpreter_path),
        },
        "writable": {
            "config": config_writable,
            "plugin_data": data_writable,
            "state": state_writable,
        },
        "config": {"observed": observed, "proposed": proposed, "diff": config_diff},
        "agent": {"path": str(target), "status": status, "version": PLUGIN_VERSION},
        "env": {"python": python, "deps": deps},
        "prerequisites": prerequisites,
        "prerequisite_fixes": prerequisite_rows,
        "key_template": key_template,
        "interpreter": interpreter_change,
        "policy": {
            "path": str(settings_path),
            "observed": policy_observed,
            "proposed": policy_proposed,
            "toggles": policy_toggles,
        },
        "workspace": str(workspace) if workspace is not None else None,
    }
    canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
    return {
        "host": selected.host.value,
        "python": python,
        "env": {"present": python is not None, "path": python},
        "deps": deps,
        "prerequisites": prerequisites,
        "prerequisite_fixes": prerequisite_rows,
        "config_path": str(config_path),
        "config_diff": config_diff,
        "agent_path": str(target),
        "agent_status": status,
        "keys_path": str(keys_path),
        "overlay_path": str(overlay_path),
        "data_root": str(selected.data_root),
        "state_root": str(selected.state_root),
        "state_writable": state_writable,
        "plugin_data_writable": data_writable,
        "config_writable": config_writable,
        "key_template": key_template,
        "interpreter": interpreter_change,
        "proposed": proposed,
        "policy_settings_path": str(settings_path),
        "policy_observed": policy_observed,
        "policy_proposed": policy_proposed,
        "policy_toggles": policy_toggles,
        "workspace": str(workspace) if workspace is not None else None,
        "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "digest_input": digest_input,
    }


def _toml_preview_value(value: object) -> str:
    """Render one observed TOML scalar without exposing unrelated config."""
    if value is None:
        return "<absent>"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _codex_config_diff(
    config_path: Path, observed: dict[str, object], proposed: dict[str, bool]
) -> str:
    """Return a bounded unified diff for only the three required Codex keys."""
    before: list[str] = []
    after: list[str] = []
    tables: dict[str, list[tuple[str, str]]] = {
        "features": [("hooks", "features.hooks"), ("multi_agent", "features.multi_agent")],
        "agents": [("enabled", "agents.enabled")],
    }
    for table_name, rows in tables.items():
        changed = [(key, qualified) for key, qualified in rows if proposed[qualified]]
        if not changed:
            continue
        before.append(f"[{table_name}]")
        after.append(f"[{table_name}]")
        for key, qualified in changed:
            current = observed[qualified]
            if current is not None:
                before.append(f"{key} = {_toml_preview_value(current)}")
            after.append(f"{key} = true")
    if not before:
        return ""
    return "".join(
        difflib.unified_diff(
            [line + "\n" for line in before],
            [line + "\n" for line in after],
            fromfile=str(config_path),
            tofile=f"{config_path} (approved)",
        )
    )


def _non_mutating_writable(path: Path) -> bool:
    """Check writability without creating, modifying, or deleting anything."""
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


def run_codex_setup(
    args: argparse.Namespace,
    *,
    find_env_python: EnvPythonFinder,
    check_deps: DepChecker,
    create_env: EnvCreator,
    install_deps: DepInstaller,
    permission_mode: str | None = None,
) -> int:
    """Render a Codex preview or apply one only with explicit consent."""
    live_permission = permission_mode or getattr(args, "permission_mode", None) or os.environ.get(
        "CODEX_PERMISSION_MODE"
    )
    if getattr(args, "apply", False) and live_permission != "bypassPermissions":
        print(
            "chinamax: Codex setup mutation requires codex --yolo "
            "(bypassPermissions); yolo disables Codex approval/sandbox enforcement; "
            "--read-only is enforced by the ChinamaX Runtime, not Codex's sandbox.",
            file=sys.stderr,
        )
        return 1
    policy_toggles = _parse_policy_toggle_args(getattr(args, "toggles", []) or [])
    plan = codex_setup_plan(
        args.workspace,
        find_env_python=find_env_python,
        check_deps=check_deps,
        policy_toggles=policy_toggles,
    )
    if getattr(args, "apply", False):
        if plan.get("prerequisites") and not all(plan["prerequisites"].values()):
            rows = plan.get("prerequisite_fixes")
            if rows is None:
                rows = prerequisite_fixes(plan["prerequisites"])
            print(
                f"chinamax: {missing_prerequisite_advice(rows)}; rerun setup after "
                "installing; the preview's prerequisite_fixes rows hold the exact commands",
                file=sys.stderr,
            )
            return 1
        digest = getattr(args, "consent_digest", None)
        if not digest:
            print(
                "chinamax: setup apply requires --consent-digest from a fresh preview",
                file=sys.stderr,
            )
            return 1
        try:
            result = apply_codex_setup(
                plan,
                digest,
                overwrite_unmanaged=getattr(args, "confirm_overwrite", False),
                install_agent=not getattr(args, "no_agent", False),
                find_env_python=find_env_python,
                check_deps=check_deps,
                create_env=create_env,
                install_deps=install_deps,
            )
        except ChinamaxError as error:
            print(f"chinamax: {error}", file=sys.stderr)
            return 1
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return 0 if result.get("ok") else 1
    plan["consent_required"] = True
    plan["warning"] = "Preview only: no Codex configuration or key files were modified."
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            "chinamax Codex setup preview (no writes)\n"
            + json.dumps(plan, indent=2, sort_keys=True)
            + "\nApprove this exact digest before applying: "
            + plan["digest"]
            + "\n"
        )
    return 0


def apply_codex_setup(
    plan: dict,
    consent_digest: str | None,
    *,
    overwrite_unmanaged: bool = False,
    install_agent: bool = True,
    context: HostContext | None = None,
    find_env_python: EnvPythonFinder | None = None,
    check_deps: DepChecker | None = None,
    create_env: EnvCreator | None = None,
    install_deps: DepInstaller | None = None,
) -> dict:
    """Apply one previously previewed Codex plan after digest validation.

    This low-level seam is intentionally non-interactive: the native skill owns
    the yes/no and second overwrite confirmations, while this function enforces
    their content-addressed boundary and performs each file mutation atomically.
    """
    if not consent_digest:
        raise ChinamaxError("Codex setup apply requires the preview consent digest")
    selected = context or current_host()
    if selected.host is not Host.CODEX:
        raise ChinamaxError("Codex setup apply requires the Codex Host")
    fresh = codex_setup_plan(
        plan.get("workspace"),
        context=selected,
        find_env_python=find_env_python,
        check_deps=check_deps,
        policy_toggles=plan.get("policy_toggles"),
    )
    if consent_digest != fresh["digest"] or plan.get("digest") != fresh["digest"]:
        raise ChinamaxError(
            "target is changing between preview and apply; render a fresh Codex setup preview"
        )
    if fresh.get("prerequisites") and not all(fresh["prerequisites"].values()):
        rows = fresh.get("prerequisite_fixes")
        if rows is None:
            rows = prerequisite_fixes(fresh["prerequisites"])
        raise ChinamaxError(missing_prerequisite_advice(rows))
    config_path = Path(fresh["config_path"])
    changed: list[str] = []
    fixes: list[dict] = []
    find_env_python = find_env_python or _find_env_python
    create_env = create_env or _create_env
    install_deps = install_deps or _install_deps
    env_python = fresh["python"]
    if fresh["key_template"]["create"] and write_key_template(selected):
        changed.append(str(profiles.keys_path(selected)))
        fixes.append({"action": "key-template", "ok": True})
    if not fresh["env"]["present"]:
        ok, detail = create_env()
        fixes.append({"action": "create-env", "ok": ok, "detail": detail})
        if not ok:
            return {"ok": False, "changed": changed, "fixes": fixes, "digest": fresh["digest"]}
        env_python = find_env_python()
    if env_python and not all(fresh["deps"].values()):
        ok, detail = install_deps(env_python)
        fixes.append({"action": "install-deps", "ok": ok, "detail": detail})
        if not ok:
            return {"ok": False, "changed": changed, "fixes": fixes, "digest": fresh["digest"]}
    if env_python and (fresh["interpreter"]["update"] or fresh["interpreter"]["recorded"] != env_python):
        _write_python_path(python_path_file(selected), env_python)
        changed.append(str(python_path_file(selected)))
    # The Policy toggles are part of the apply transaction: write when the file is
    # absent/malformed or the resolved values change (ADR 0016 amended 0.7.0).
    from chinamax import policy as _policy

    policy_observed = fresh["policy_observed"]
    policy_proposed = fresh["policy_proposed"]
    policy_current = {name: policy_observed[name] for name in _POLICY_TOGGLE_KEYS}
    if (
        not policy_observed["present"]
        or policy_observed["malformed"]
        or policy_proposed != policy_current
    ):
        _policy.write_policy_settings(selected, fresh["policy_toggles"])
        changed.append(fresh["policy_settings_path"])
    if any(fresh["proposed"].values()):
        try:
            import tomlkit
        except ImportError as error:
            raise ChinamaxError(
                "tomlkit is required before Codex config can be edited; rerun setup"
            ) from error
        try:
            original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
            document = tomlkit.parse(original) if original else tomlkit.document()
        except (OSError, ValueError) as error:
            raise ChinamaxError(
                f"Codex config {config_path} is not valid TOML; no config mutation applied"
            ) from error
        features = document.get("features")
        if features is None:
            features = tomlkit.table()
            document["features"] = features
        agents = document.get("agents")
        if agents is None:
            agents = tomlkit.table()
            document["agents"] = agents
        for key, table in (
            ("hooks", features),
            ("multi_agent", features),
            ("enabled", agents),
        ):
            if table.get(key) is not True:
                table[key] = True
                changed.append(key)
        if config_path.exists():
            shutil.copy2(config_path, config_path.with_name(config_path.name + ".bak"))
        _atomic_text_write(config_path, tomlkit.dumps(document))
    target = Path(fresh["agent_path"])
    if install_agent:
        if fresh["agent_status"] == "unmanaged-collision" and not overwrite_unmanaged:
            raise ChinamaxError(
                f"unmanaged Codex agent collision at {target}; preview and confirm overwrite"
            )
        if fresh["agent_status"] != "managed-current":
            _atomic_text_write(target, compile_codex_agent(_agent_source_path(selected)))
            changed.append(str(target))
    result = {"ok": True, "changed": changed, "digest": fresh["digest"], "fixes": fixes}
    if not install_agent:
        result["warning"] = f"Codex native agent was not installed at {target}; rerun setup to install it."
    return result


def _workspace_state_dir(root: Path, workspace: str | Path | None) -> Path:
    """Return the per-workspace state dir under ``root``, or the root itself.

    An unresolvable workspace still yields a graded directory — the root — rather
    than a failed diagnosis, so setup reports on a fresh checkout with no repo.
    """
    try:
        return root / state.workspace_key(state.resolve_workspace_root(workspace))
    except ChinamaxError:
        return root


def _state_writable(directory: Path) -> bool:
    """Report whether a state directory can be created and written under."""
    try:
        state.make_dir(directory)
        probe = directory / ".chinamax-setup-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _profile_rows(context: HostContext | None = None) -> list[dict]:
    """Report every Profile's key env-var name and PRESENT/MISSING — never a value."""
    resolved = profiles.load_profiles(context)
    keys = profiles.load_keys(context)
    rows = []
    for name in sorted(resolved):
        profile = resolved[name]
        present = bool(keys.get(profile.api_key_env, "").strip())
        rows.append(
            {
                "name": name,
                "key_env": profile.api_key_env,
                "key": "PRESENT" if present else "MISSING",
            }
        )
    return rows


def _maybe_record_python(env_python: str, context: HostContext | None = None) -> None:
    """Record the resolved env python, re-recording only a stale entry.

    A stored path that still resolves to an executable is trusted (the Bridge and
    shims already read it); a missing or no-longer-resolving one is re-recorded.
    """
    target = python_path_file(context)
    stored = _read_recorded_python(target)
    if stored is not None and _is_executable(stored):
        return
    _write_python_path(target, env_python)


def _read_recorded_python(target: Path) -> str | None:
    """Return the recorded interpreter path, or None when there is none."""
    try:
        value = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write_python_path(target: Path, value: str) -> None:
    """Record one absolute interpreter path atomically (tmp+rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    state.atomic_replace(temporary, target)


def _is_executable(path: str) -> bool:
    """Report whether a path is an absolute, existing, executable file."""
    if not path:
        return False
    absolute = ntpath.isabs(path) if sys.platform == "win32" else os.path.isabs(path)
    if not absolute or not os.path.isfile(path):
        return False
    if sys.platform == "win32":
        return Path(path).suffix.lower() in {".exe", ".bat", ".cmd"}
    return os.access(path, os.X_OK)


def _find_env_python() -> str | None:
    """Resolve the ``chinamax`` env's absolute python, or None when absent.

    Prefers the conventional miniconda path, then asks ``conda`` for the env's
    interpreter without hardcoding conda's own location; anything that is not an
    executable absolute path is treated as absent.
    """
    base = Path.home() / "miniconda3"
    candidate = (
        base / "envs" / ENV_NAME / "python.exe"
        if sys.platform == "win32"
        else base / "envs" / ENV_NAME / "bin" / "python"
    )
    if _is_executable(str(candidate)):
        return str(candidate)
    if sys.platform == "win32":
        conda_commands = [
            str(base / "Scripts" / "conda.exe"),
            str(base / "condabin" / "conda.bat"),
            "conda",
        ]
    else:
        conda_commands = ["conda"]
    # Prepend the absolute conda _find_conda resolves (~/miniconda3 first, PATH
    # fallback) WITHOUT collapsing the list: keep the trailing bare "conda" so a
    # machine whose chinamax env lives under a different conda (anaconda3/miniforge
    # on PATH) is still found. Drop None and duplicates; keep the try-next loop.
    resolved_conda = _find_conda()
    if resolved_conda is not None:
        conda_commands = list(dict.fromkeys([resolved_conda, *conda_commands]))
    finished = None
    for conda in conda_commands:
        if conda != "conda" and not Path(conda).exists():
            continue
        try:
            candidate = subprocess.run(
                [conda, "run", "-n", ENV_NAME, "python", "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if candidate.returncode == 0:
            finished = candidate
            break
    if finished is None:
        return None
    output = finished.stdout.strip().splitlines()
    resolved = output[-1].strip() if output else ""
    return resolved if finished.returncode == 0 and _is_executable(resolved) else None


def _create_env() -> tuple[bool, str]:
    """Create the env with ``conda create -y -n chinamax python=3.12``.

    Bounded and reported once — a missing conda is advice, never a retry loop.
    """
    try:
        finished = subprocess.run(
            [_find_conda() or "conda", "create", "-y", "-n", ENV_NAME, "python=3.12"],
            capture_output=True,
            text=True,
            timeout=FIX_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return False, "conda not found — install Miniconda, then re-run /chinamax:setup"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"conda create failed: {exc}"
    if finished.returncode != 0:
        return False, _output_tail(finished.stderr or finished.stdout)
    return True, ""


def _install_deps(env_python: str) -> tuple[bool, str]:
    """Install the deps: ``pip install -e '<repo>[test]'`` under the env python.

    The ``[test]`` extra pulls ``anthropic`` and ``pytest``, so one install
    satisfies every entry in DEPS — the doctor's own check.
    """
    repo = source_repo_path()
    if not os.path.isabs(repo):
        return False, (
            "source repo not found — run: "
            f"{env_python} -m pip install -e '<the chinamax repo>[test]'"
        )
    try:
        finished = subprocess.run(
            [env_python, "-m", "pip", "install", "-e", f"{repo}[test]"],
            capture_output=True,
            text=True,
            timeout=FIX_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"pip install failed: {exc}"
    if finished.returncode != 0:
        return False, _output_tail(finished.stderr or finished.stdout)
    return True, ""


def _output_tail(text: str, limit: int = 2000) -> str:
    """Return the last ``limit`` characters of a fix subprocess's output."""
    return (text or "").strip()[-limit:]


def _check_deps(env_python: str) -> dict[str, bool]:
    """Report which of DEPS import under the resolved env python."""
    return {name: _can_import(env_python, name) for name in required_deps()}


def _can_import(env_python: str, module: str) -> bool:
    """Report whether ``module`` imports under ``env_python``."""
    try:
        finished = subprocess.run(
            [env_python, "-c", f"import {module}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return finished.returncode == 0
