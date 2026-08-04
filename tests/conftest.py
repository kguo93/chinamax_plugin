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
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from chinamax import state
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
    for name in AMBIENT_VARIABLES:
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
    "identity",
    "job_artifacts",
    "job_leftovers",
    "loop_config",
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
    "write_keys",
    "write_overlay",
]
