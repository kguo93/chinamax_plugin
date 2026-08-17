"""Shared fixtures: a keyless HOME, the fake provider, and a runnable Job.

Every test runs under a temporary HOME holding a synthetic ``model-keys.env``,
with the ambient ``ANTHROPIC_*`` variables removed — so the suite is keyless and
endpoint-clean by default rather than by opting in.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chinamax import confinement, state
from chinamax.__main__ import main, run_exec
from chinamax.liveness import LoopConfig
from chinamax.transcript import read_messages
from fake_provider import FakeProvider, text_block, tool_use_block, turn

PROFILE = "deepseek"
SYNTHETIC_KEYS = {
    "DEEPSEEK_API_KEY": "sk-fake-deepseek",
    "MIMO_API_KEY": "sk-fake-mimo",
    "GLM_API_KEY": "sk-fake-glm",
    "MINIMAX_API_KEY": "sk-fake-minimax",
    "KIMI_API_KEY": "sk-fake-kimi",
    "QWEN_API_KEY": "sk-fake-qwen",
}
AMBIENT_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

BASH_COMMAND = "echo hello > out.txt && cat out.txt"
BASH_TOOL_USE_ID = "toolu_bash_1"
REPORT_TOOL_USE_ID = "toolu_report_1"
REPORT_PAYLOAD = {
    "response": (
        "Wrote out.txt in the workspace and read it back; `cat out.txt` prints "
        "hello. The workspace was empty before this job."
    ),
}

#: Sentinel: drop this field from the generated job spec.
OMIT = object()


def bash_then_report_script() -> list[dict]:
    """Script a bash turn followed by the terminal report_result turn."""
    return [
        turn([tool_use_block(BASH_TOOL_USE_ID, "bash", {"command": BASH_COMMAND})]),
        report_turn(),
    ]


def report_turn() -> dict:
    """Script the terminal report_result turn."""
    return turn([tool_use_block(REPORT_TOOL_USE_ID, "report_result", REPORT_PAYLOAD)])


def tool_script(*calls: tuple[str, dict]) -> list[dict]:
    """Script one turn per ``(tool name, input)`` call, then the terminal turn.

    One call per turn, so a test can assert that the loop carried on to the NEXT
    turn after an observation — which is the whole point of tools that fail
    without ending the Job.
    """
    return [
        turn([tool_use_block(f"toolu_{index}", name, value)])
        for index, (name, value) in enumerate(calls)
    ] + [report_turn()]


def bash_script(*commands: str) -> list[dict]:
    """Script one bash turn per command, then the terminal turn."""
    return tool_script(*(("bash", {"command": command}) for command in commands))


def tool_results(messages: list[dict]) -> list[dict]:
    """Return every tool_result block in a message sequence, in order."""
    return [
        block
        for message in messages
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


class Sleeper:
    """Records the ladder's backoff sleeps instead of performing them."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        """Record one sleep."""
        self.sleeps.append(seconds)


class SteppingClock:
    """A clock that jumps a fixed simulated interval on every read."""

    def __init__(self, start: float = 1_700_000_000.0, step: float = 0.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        """Advance and return the simulated time."""
        self.now += self.step
        return self.now


def identity(value: float) -> float:
    """Jitter seam for tests: expectations never couple to a PRNG sequence."""
    return value


def loop_config(sleeper: Sleeper, clock: SteppingClock | None = None) -> LoopConfig:
    """Build a supervision config whose backoff is deterministic and instant."""
    return LoopConfig(
        clock=SteppingClock() if clock is None else clock,
        sleeper=sleeper,
        jitter=identity,
    )


def reporter_events(err: str) -> list[dict]:
    """Parse the structured JSON lines the reporter wrote to stderr.

    Filters by shape rather than reading every line: prose errors and any
    stdlib traceback share the stream, and neither is an event.
    """
    events = []
    for line in err.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "event" in record:
            events.append(record)
    return events


def events_named(err: str, name: str) -> list[dict]:
    """Return the reporter events of one kind, in order."""
    return [event for event in reporter_events(err) if event["event"] == name]


def write_overlay(home: Path, rows: list[dict]) -> None:
    """Write the user Profile overlay into a temporary HOME."""
    (home / ".claude" / "chinamax-profiles.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


def write_keys(home: Path, keys: dict[str, str]) -> None:
    """Write a bare ``NAME=value`` key file into a temporary HOME."""
    (home / ".claude" / "model-keys.env").write_text(
        "".join(f"{name}={value}\n" for name, value in keys.items()), encoding="utf-8"
    )


@dataclass
class JobEnv:
    """A dispatchable Job bound to one fake provider."""

    fake: FakeProvider
    workspace: Path
    transcript_path: Path
    result_path: Path
    spec_path: Path
    profile: str
    prompt: str

    @property
    def requests(self) -> list[dict]:
        """Return the requests the fake provider has received."""
        return self.fake.requests

    def spec(self, **overrides: object) -> dict:
        """Build a job spec, overriding or (with ``OMIT``) dropping fields."""
        fields: dict[str, object] = {
            "workspace": str(self.workspace),
            "profile": self.profile,
            "prompt": self.prompt,
            "transcript_path": str(self.transcript_path),
            "result_path": str(self.result_path),
        }
        fields.update(overrides)
        return {name: value for name, value in fields.items() if value is not OMIT}

    def run(self, spec: dict | None = None, config: LoopConfig | None = None) -> int:
        """Run the Job through the exec seam and return its exit code.

        Args:
            spec: The job spec to write; the default one when omitted.
            config: Supervision seams (clock, sleeper, jitter) to inject. The
                shared `run_exec` entry takes them, so a test that needs
                deterministic backoff still runs the real entry point.
        """
        self.spec_path.write_text(
            json.dumps(self.spec() if spec is None else spec), encoding="utf-8"
        )
        if config is None:
            return main(["exec", str(self.spec_path)])
        return run_exec(self.spec_path, config=config)

    def result(self) -> dict:
        """Return the stored result, parsed."""
        return json.loads(self.result_path.read_text(encoding="utf-8"))

    def observations(self) -> list[dict]:
        """Return every tool_result the Job produced, from its durable Thread."""
        return tool_results(read_messages(self.transcript_path))

    def tree(self) -> list[str]:
        """Return every path under the workspace, relative and sorted.

        Walked with ``followlinks=False`` rather than ``rglob``, which follows
        directory symlinks on Python 3.12 and would walk out of the workspace.
        """
        found = []
        for directory, dirnames, filenames in os.walk(self.workspace, followlinks=False):
            for name in dirnames + filenames:
                found.append(os.path.relpath(os.path.join(directory, name), self.workspace))
        return sorted(found)


def aged(seconds: float) -> str:
    """Return an ISO-8601 UTC timestamp ``seconds`` in the past.

    Stale detection is graded by AGEING a record's heartbeat rather than by
    sleeping the grace out, so a 60 s window costs a test nothing.
    """
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat()


def dead_pid() -> int:
    """Return the pid of a process that has definitively exited and been reaped."""
    finished = subprocess.Popen(["sleep", "0"])
    finished.wait()
    return finished.pid


def build_record(
    store,
    *,
    workspace: Path,
    status: str = state.STATUS_QUEUED,
    prompt: str = "Do the task.",
    profile: str = PROFILE,
    write: bool = True,
    model: str | None = None,
    pid: int | None = None,
    pid_start_time: int | None = None,
    updated_at: str | None = None,
    completed_at: str | None = None,
    result: dict | None = None,
    session_id: str | None = None,
    bridge_name: str | None = None,
    lineage_root: str | None = None,
    supervised_at: str | None = None,
    supervision_timeout_ms: int | None = None,
) -> str:
    """Publish one Job record directly, with no dispatcher and no worker.

    For the states a live worker will not hold still in — an aged heartbeat, a
    dead recorded pid, a `queued` record nobody ever claimed. Every write still
    goes through the store's own locked compare-and-swap updater. ``session_id``
    and ``bridge_name`` set the owning-session/Bridge fields at creation;
    ``lineage_root`` sets the resume-lineage field for the lineage-scoped tests;
    ``supervised_at``/``supervision_timeout_ms`` set the Bridge-supervision
    heartbeat for the stale-supervision reap tests (a backdated ``supervised_at``
    goes through the same ``touch=False`` update the ``updated_at`` parameter uses).
    ``model`` pins the dispatch's model onto the record's ``request`` block, for
    the row/detail and resume-pin tests.
    """
    store.ensure()
    job_id = store.reserve_id()
    store.create(
        state.new_record(
            job_id,
            prompt=prompt,
            profile=profile,
            write=write,
            workspace_root=workspace,
            log_file=store.log_path(job_id),
            model=model,
            originating_session=session_id,
            bridge_name=bridge_name,
        )
    )
    changes: dict = {"status": status}
    if status != state.STATUS_QUEUED:
        changes["startedAt"] = state.utc_now()
    for name, value in (
        ("pid", pid),
        ("pidStartTime", pid_start_time),
        ("completedAt", completed_at),
        ("result", result),
        ("lineageRoot", lineage_root),
        ("supervisionTimeoutMs", supervision_timeout_ms),
    ):
        if value is not None:
            changes[name] = value
    store.update(job_id, changes, expect={state.STATUS_QUEUED})
    if updated_at is not None:
        store.update(job_id, {"updatedAt": updated_at}, touch=False)
    if supervised_at is not None:
        store.update(job_id, {"supervisedAt": supervised_at}, touch=False)
    return job_id


def job_artifacts(store, job_id: str) -> list[Path]:
    """Return all SIX per-Job artifacts of the authoritative state layout."""
    return [
        store.record_path(job_id),
        store.log_path(job_id),
        store.spawn_log_path(job_id),
        store.transcript_path(job_id),
        store.result_path(job_id),
        store.steer_dir(job_id),
    ]


def write_artifacts(store, job_id: str) -> None:
    """Create every per-Job artifact, steer queue and nested consumed/ included."""
    for path in job_artifacts(store, job_id)[1:-1]:
        state.precreate(path).write_text("{}\n", encoding="utf-8")
    consumed = store.steer_dir(job_id) / "consumed"
    state.make_dir(consumed)
    (consumed / "0000000000-0000-aaaaaa.md").write_text("drained", encoding="utf-8")
    (store.steer_dir(job_id) / "0000000001-0000-bbbbbb.md").write_text(
        "queued", encoding="utf-8"
    )


def job_leftovers(store, job_id: str) -> list[str]:
    """Return every entry under ``jobs/`` still belonging to one Job.

    Matched by NAME PREFIX rather than against a fixed list, so a seventh
    artifact added to the layout fails a pruning assertion loudly instead of
    quietly leaking orphans.
    """
    try:
        entries = list(os.scandir(store.jobs_dir))
    except OSError:
        return []
    return sorted(entry.name for entry in entries if entry.name.startswith(job_id))


# ── Worker Host-policy fixtures (ADR 0016) ─────────────────────────────────────
# Hook scripts, settings files, and the stdio MCP fixture server all live under
# tmp roots and write observable evidence files with real exit codes, so no test
# ever touches the operator's real settings (a hard requirement of the plan).

#: A tiny hand-rolled MCP stdio server: line-delimited JSON-RPC over stdin/stdout,
#: version-independent of the `mcp` SDK's own server API. Reads its tool list from
#: ``CHINAMAX_FIXTURE_TOOLS`` (JSON) or defaults to a single ``echo`` tool, and an
#: optional ``CHINAMAX_FIXTURE_DELAY`` (seconds) stalls each ``tools/call`` so a
#: per-call-timeout test can bite. Echoes ``<tool>: <json-args>`` back as text.
_MCP_FIXTURE_SERVER = '''\
import json, os, sys, time

def _tools():
    raw = os.environ.get("CHINAMAX_FIXTURE_TOOLS")
    if raw:
        return json.loads(raw)
    return [{"name": "echo", "description": "Echo the arguments back.",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}}]

def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

def main():
    tools = _tools()
    names = {tool["name"] for tool in tools}
    delay = float(os.environ.get("CHINAMAX_FIXTURE_DELAY", "0") or 0)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            pv = msg.get("params", {}).get("protocolVersion", "2025-06-18")
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": pv,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fixture", "version": "0.0.0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            if delay:
                time.sleep(delay)
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name in names:
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": name + ": " + json.dumps(args, sort_keys=True)}],
                    "isError": False}})
            else:
                _send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601, "message": "unknown tool"}})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "method not found"}})

if __name__ == "__main__":
    main()
'''


def mcp_server_script(directory: Path) -> Path:
    """Materialize the stdio MCP fixture server under ``directory``."""
    script = Path(directory) / "mcp_fixture_server.py"
    script.write_text(_MCP_FIXTURE_SERVER, encoding="utf-8")
    return script


def mcp_server_entry(script: Path, *, env: dict[str, str] | None = None, cwd: str | None = None) -> dict:
    """Build one `.mcp.json`/`mcpServers` entry pointing at the fixture server."""
    entry: dict = {"command": sys.executable, "args": [str(script)]}
    if env is not None:
        entry["env"] = env
    if cwd is not None:
        entry["cwd"] = cwd
    return entry


def write_mcp_config(path: Path, servers: dict) -> None:
    """Write a project `.mcp.json` with the given ``mcpServers`` mapping."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def write_hook_script(
    directory: Path,
    name: str,
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    evidence: Path | None = None,
    sleep_s: float = 0.0,
) -> str:
    """Materialize a bash hook script and return the settings ``command`` string.

    The script drains its stdin (the harness payload) into ``evidence`` when
    given — so a test can assert the translated ``tool_name``/``tool_input`` the
    hook actually saw — then emits ``stdout``/``stderr`` and exits ``exit_code``.
    """
    Path(directory).mkdir(parents=True, exist_ok=True)
    script = Path(directory) / f"hook_{name}.sh"
    lines = ["#!/usr/bin/env bash", "set -u"]
    if evidence is not None:
        lines.append(f"cat > {shlex.quote(str(evidence))} 2>/dev/null || true")
    else:
        lines.append("cat > /dev/null 2>/dev/null || true")
    if sleep_s:
        lines.append(f"sleep {sleep_s}")
    if stdout:
        lines.append(f"printf %s {shlex.quote(stdout)}")
    if stderr:
        lines.append(f"printf %s {shlex.quote(stderr)} 1>&2")
    lines.append(f"exit {exit_code}")
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"bash {shlex.quote(str(script))}"


def hook_group(command: str, *, matcher: str | None = None, timeout: float | None = None) -> dict:
    """Build one Claude settings hook group carrying a single command hook."""
    hook: dict = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    group: dict = {"hooks": [hook]}
    if matcher is not None:
        group["matcher"] = matcher
    return group


def write_claude_settings(
    path: Path,
    *,
    pre: list[dict] | None = None,
    post: list[dict] | None = None,
    stop: list[dict] | None = None,
    disable_all: bool = False,
) -> None:
    """Write a Claude ``settings.json`` with the given hook groups."""
    hooks: dict = {}
    if pre is not None:
        hooks["PreToolUse"] = pre
    if post is not None:
        hooks["PostToolUse"] = post
    if stop is not None:
        hooks["Stop"] = stop
    data: dict = {}
    if hooks:
        data["hooks"] = hooks
    if disable_all:
        data["disableAllHooks"] = True
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data), encoding="utf-8")


def policy_spec(
    workspace: Path,
    transcript_path: Path,
    *,
    job_id: str = "task-test-aaaaaa",
    host: str = "claude",
    mcp: list[str] | None = None,
    memory_enabled: bool = True,
    hooks_enabled: bool = True,
) -> types.SimpleNamespace:
    """A minimal spec-shaped object for direct `Policy.build` unit calls.

    The Policy toggles default ON so hook/Memory behavior under a direct
    ``Policy.build`` call stays exercised; the settings.json default-OFF polarity
    is covered through the dispatch/exec seams instead (ADR 0016 amended 0.7.0).
    """
    return types.SimpleNamespace(
        workspace=str(workspace),
        transcript_path=str(transcript_path),
        job_id=job_id,
        host=host,
        mcp=mcp,
        memory_enabled=memory_enabled,
        hooks_enabled=hooks_enabled,
    )


def write_policy_settings(
    *, memory: bool = False, hooks: bool = False, mcp: bool = False, host: str = "claude"
) -> None:
    """Write the per-Host ``settings.json`` toggles through production resolution.

    Resolves the settings root ITSELF the way production does — a fresh
    ``HostContext`` off the current fixture env, so ``dispatch_env`` lands under
    its ``CLAUDE_PLUGIN_DATA`` state root and ``job_env`` under the temp HOME
    fallback — rather than a caller-passed root that could drift. ``host="codex"``
    resolves through the Codex family, never the Claude root.
    """
    from chinamax import policy
    from chinamax.host import Host, HostContext

    context = HostContext.from_host(Host(host))
    policy.write_policy_settings(context, {"memory": memory, "hooks": hooks, "mcp": mcp})


def raw_tools_array(raw_body: bytes) -> bytes:
    """Extract the balanced ``"tools":[…]`` byte slice from a raw request body.

    A true byte-level comparison (not a parsed object, which cannot see key-order
    drift): finds the tools array and returns its exact bytes, so two turns'
    arrays can be asserted byte-identical.
    """
    marker = b'"tools":'
    start = raw_body.index(marker) + len(marker)
    assert raw_body[start:start + 1] == b"[", raw_body[start:start + 1]
    depth = 0
    for index in range(start, len(raw_body)):
        char = raw_body[index : index + 1]
        if char == b"[":
            depth += 1
        elif char == b"]":
            depth -= 1
            if depth == 0:
                return raw_body[start : index + 1]
    raise AssertionError("unbalanced tools array in request body")


def memory_block_paths(text: str) -> list[str]:
    """Return the Memory-file paths declared inside an injection block in ``text``."""
    from chinamax.policy import _MEMORY_FILE_PREFIX, _MEMORY_FILE_SUFFIX, _MEMORY_OPEN

    paths: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line == _MEMORY_OPEN:
            in_block = True
        elif in_block and line.startswith(_MEMORY_FILE_PREFIX) and line.endswith(_MEMORY_FILE_SUFFIX):
            paths.append(line[len(_MEMORY_FILE_PREFIX) : -len(_MEMORY_FILE_SUFFIX)])
    return paths


def assert_wire_shape(messages: list[dict]) -> None:
    """Assert a request's history is one a strict endpoint would accept.

    No two consecutive same-role messages — the Profiles target third-party
    endpoints whose tolerance for those is not guaranteed — and every
    ``tool_use`` answered by a ``tool_result`` carrying its id.
    """
    roles = [message["role"] for message in messages]
    assert all(one != other for one, other in zip(roles, roles[1:])), roles
    used = {
        block["id"]
        for message in messages
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    answered = {block["tool_use_id"] for block in tool_results(messages)}
    assert not used - answered, sorted(used - answered)


def wait_for(predicate, timeout_s: float = 60.0, interval_s: float = 0.05) -> bool:
    """Poll a predicate until it holds or the bound expires.

    Args:
        predicate: Called repeatedly; the wait ends when it returns truthy.
        timeout_s: The bound.
        interval_s: The poll interval.

    Returns:
        Whether the predicate held before the bound.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return bool(predicate())


def wait_for_status(store, job_id: str, statuses, timeout_s: float = 90.0) -> dict:
    """Wait until a Job's record reaches one of ``statuses`` and return it.

    Returns an empty dict when the record never became readable, so a caller
    reaping an unknown store does not fail on a Job it does not own.
    """
    wanted = set(statuses)
    wait_for(lambda: (store.try_read(job_id) or {}).get("status") in wanted, timeout_s)
    return store.try_read(job_id) or {}


@dataclass
class DispatchEnv:
    """A workspace whose dispatches land in a temp state dir.

    Every dispatch spawns the REAL detached worker against the fake provider —
    no mocked process layer anywhere (the jobs PRD's testing decisions).
    """

    home: Path
    workspace: Path
    plugin_data: Path
    state_dir: Path
    start_provider: object
    providers: dict = field(default_factory=dict)

    def bind(self, script: list[dict], profile: str = PROFILE) -> FakeProvider:
        """Start a fake provider and point one Profile at it through the overlay."""
        provider = self.start_provider(script)
        self.providers[profile] = provider
        write_overlay(
            self.home,
            [
                {"name": name, "base_url": bound.base_url}
                for name, bound in self.providers.items()
            ],
        )
        return provider

    @property
    def store(self):
        """Return the store this workspace's dispatches land in."""
        return state.JobStore(self.state_dir, workspace_root=self.workspace)

    def dispatch(
        self,
        *extra: str,
        prompt: str = "Do the task.",
        profile: str = PROFILE,
        workspace: Path | None = None,
    ) -> tuple[int, str]:
        """Run the ``task`` verb and return ``(exit code, printed job id)``."""
        argv = [
            "task",
            "--profile",
            profile,
            "--workspace",
            str(self.workspace if workspace is None else workspace),
            *extra,
            "--",
            prompt,
        ]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(argv)
        return code, stream.getvalue().strip()

    def reap(self) -> None:
        """Let every worker this test started finish, then kill what is left.

        Walks every store under the temp state root, so a dispatch aimed at
        another workspace is reaped too. Runs BEFORE the fake provider is torn
        down (fixture ordering), so a still-running worker never spends the
        retry ladder on a dead endpoint.
        """
        for directory in sorted((self.plugin_data / "state").glob("*")):
            if not directory.is_dir():
                continue
            store = state.JobStore(directory)
            for job_id in store.job_ids():
                record = store.try_read(job_id) or {}
                if not self._is_our_live_worker(record):
                    continue
                record = wait_for_status(store, job_id, state.TERMINAL_STATUSES, 45.0)
                if self._is_our_live_worker(record):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(record["pid"], signal.SIGKILL)

    @staticmethod
    def _is_our_live_worker(record: dict) -> bool:
        """Report whether a record names a worker this test really started.

        The recorded ``pidStartTime`` must still match the live process — the
        same pid-reuse guard jobs/02 grades with, used here so teardown can
        never signal an unrelated process that inherited the pid.
        """
        if record.get("status") in state.TERMINAL_STATUSES:
            return False
        pid, start = record.get("pid"), record.get("pidStartTime")
        if not isinstance(pid, int) or start is None:
            return False
        return state.read_pid_start_time(pid) == start


@pytest.fixture
def dispatch_env(tmp_path, keyless_home, start_fake_provider, monkeypatch):
    """Return a factory for a workspace whose state dir is under the temp tree."""
    created: list[DispatchEnv] = []

    def _make(script: list[dict] | None = None) -> DispatchEnv:
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        plugin_data = tmp_path / "plugin-data"
        monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
        monkeypatch.delenv(state.SESSION_ID_VARIABLE, raising=False)
        monkeypatch.delenv(state.WORKER_PYTHON_VARIABLE, raising=False)
        root = state.resolve_workspace_root(workspace)
        env = DispatchEnv(
            home=keyless_home,
            workspace=workspace,
            plugin_data=plugin_data,
            state_dir=state.state_root() / state.workspace_key(root),
            start_provider=start_fake_provider,
        )
        if script is not None:
            env.bind(script)
        created.append(env)
        return env

    yield _make
    for env in created:
        env.reap()


@pytest.fixture(autouse=True)
def keyless_home(tmp_path_factory, monkeypatch) -> Path:
    """Point HOME at a temporary dir with synthetic keys and no ambient endpoint."""
    home = tmp_path_factory.mktemp("home")
    (home / ".claude").mkdir()
    write_keys(home, SYNTHETIC_KEYS)
    monkeypatch.setenv("HOME", str(home))
    # Existing Runtime seam tests call library entrypoints directly; bind them
    # explicitly to the Claude adapter while dedicated Host tests exercise the
    # fail-closed no-marker behavior.
    monkeypatch.setenv("CHINAMAX_HOST", "claude")
    # Pin the Claude managed-settings discovery root under the temp HOME, at a
    # path that does not exist — the "no managed settings" result the suite relies
    # on is now hermetic by construction, never the operator's real
    # /etc/claude-code/managed-settings.json happening to be absent (ADR 0016).
    monkeypatch.setenv(
        "CHINAMAX_MANAGED_SETTINGS", str(home / ".managed" / "managed-settings.json")
    )
    for name in AMBIENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    # Strip inherited plugin-data roots so the state root falls back UNDER this
    # test's temp HOME. Without this a suite run inside an installed plugin (where
    # CLAUDE_PLUGIN_DATA points at the operator's real ~/.claude data) would read
    # AND write the operator's real per-Host settings.json (ADR 0016 amended
    # 0.7.0). `dispatch_env`/`isolated` set their own temp roots after this.
    for name in ("CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "PLUGIN_DATA", "PLUGIN_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def start_fake_provider():
    """Return a factory starting per-test fake providers, torn down afterwards."""
    started: list[FakeProvider] = []

    def _start(script: list[dict], snapshot_path: Path | None = None) -> FakeProvider:
        provider = FakeProvider(script, snapshot_path=snapshot_path)
        provider.start()
        started.append(provider)
        return provider

    yield _start
    for provider in started:
        provider.stop()


@pytest.fixture
def job_env(tmp_path, keyless_home, start_fake_provider):
    """Return a factory building a Job bound to a scripted fake provider."""

    def _make(script: list[dict], profile: str = PROFILE, prompt: str = "Do the task.") -> JobEnv:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        transcript_path = tmp_path / "job.thread.jsonl"
        provider = start_fake_provider(script, snapshot_path=transcript_path)
        # The overlay is the only endpoint seam, exactly as in production.
        write_overlay(keyless_home, [{"name": PROFILE, "base_url": provider.base_url}])
        return JobEnv(
            fake=provider,
            workspace=workspace,
            transcript_path=transcript_path,
            result_path=tmp_path / "job.result.json",
            spec_path=tmp_path / "spec.json",
            profile=profile,
            prompt=prompt,
        )

    return _make


@pytest.fixture
def outside_root():
    """A throwaway directory OUTSIDE both permitted roots, for escape targets.

    Created under ``/var/tmp`` (the temp dir is now the Scratch root) and
    realpath'd so the sibling-prefix ``startswith`` trap and the tests' own
    ``os.path.relpath`` climbs stay honest on macOS, where ``/var/tmp`` is a
    symlink. Skips wherever no such outside root exists — on native Windows, or
    where ``/var/tmp`` falls under the Scratch root (e.g. ``TMPDIR=/var/tmp``) or
    is unwritable — so the dual-root escape net runs only on POSIX (the
    mocked-new-Platform stance), and the Windows-unreachable ``os.path.relpath``
    calls the dependent tests make never fire.
    """
    if not Path("/var/tmp").exists():
        pytest.skip("no /var/tmp outside the scratch root on this platform")
    if Path(os.path.realpath("/var/tmp")).is_relative_to(confinement.SCRATCH_ROOT):
        pytest.skip("/var/tmp lies under the scratch root (e.g. TMPDIR=/var/tmp)")
    try:
        created = tempfile.mkdtemp(prefix="chinamax-outside-", dir="/var/tmp")
    except OSError:
        pytest.skip("/var/tmp is not writable")
    directory = Path(os.path.realpath(created))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "BASH_COMMAND",
    "BASH_TOOL_USE_ID",
    "DispatchEnv",
    "JobEnv",
    "OMIT",
    "PROFILE",
    "REPORT_PAYLOAD",
    "REPORT_TOOL_USE_ID",
    "SYNTHETIC_KEYS",
    "Sleeper",
    "SteppingClock",
    "aged",
    "assert_wire_shape",
    "bash_script",
    "bash_then_report_script",
    "build_record",
    "dead_pid",
    "events_named",
    "hook_group",
    "identity",
    "job_artifacts",
    "job_leftovers",
    "loop_config",
    "mcp_server_entry",
    "mcp_server_script",
    "memory_block_paths",
    "policy_spec",
    "raw_tools_array",
    "report_turn",
    "reporter_events",
    "text_block",
    "tool_results",
    "tool_script",
    "tool_use_block",
    "turn",
    "wait_for",
    "wait_for_status",
    "write_artifacts",
    "write_claude_settings",
    "write_keys",
    "write_mcp_config",
    "write_hook_script",
    "write_overlay",
    "write_policy_settings",
]
