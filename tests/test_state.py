"""The durable store: layout, the derived index, permissions, and the schema."""

from __future__ import annotations

import argparse
import json
import os
import subprocess

import pytest

from chinamax import state
from chinamax.__main__ import build_parser, main
from conftest import PROFILE, bash_then_report_script, report_turn, wait_for_status

#: A ``/proc/<pid>/stat`` line whose comm carries spaces AND parentheses. Field
#: 22 is 987654; a naive ``text.split()[21]`` reads 17 instead, which is exactly
#: the wrong start-time that would feed jobs/02's kill decision.
STAT_WITH_PARENS = (
    "4242 (weird (comm) name) "
    + " ".join(["S"] + [str(number) for number in range(4, 22)])
    + " 987654 0 0\n"
)


def _subparsers(parser):
    """Return a parser's subcommand action, for arguing about the whole CLI."""
    return next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )


def make_job(store, prompt: str = "Do the task.") -> str:
    """Reserve and publish one queued record, returning its id."""
    job_id = store.reserve_id()
    store.create(
        state.new_record(
            job_id,
            prompt=prompt,
            profile=PROFILE,
            write=True,
            workspace_root=store.path,
            log_file=store.log_path(job_id),
        )
    )
    return job_id


def test_index_rebuilt_from_records(dispatch_env, tmp_path):
    """Records are the source of truth; a lost index rebuilds from filenames."""
    env = dispatch_env()
    store = env.store.ensure()
    first = make_job(store, "first")
    second = make_job(store, "second")
    # A result artifact beside its record: the bare `jobs/*.json` glob this
    # scan must NOT be would register it as a phantom third Job.
    store.result_path(first).write_text(json.dumps({"outcome": "completed"}), encoding="utf-8")

    for wipe in (lambda: store.index_path.unlink(), lambda: store.index_path.write_text("")):
        wipe()
        assert store.job_ids() == sorted([first, second])
        assert json.loads(store.index_path.read_text(encoding="utf-8"))["jobs"] == sorted(
            [first, second]
        )

    records, malformed = store.load_records()
    assert malformed == []
    assert sorted(record["title"] for record in records) == ["first", "second"]


def test_workspace_key_from_subdirectory(dispatch_env, tmp_path):
    """Dispatching from a subdirectory resolves to the repo's single state dir."""
    env = dispatch_env(bash_then_report_script())
    repo = tmp_path / "repo"
    (repo / "sub" / "deeper").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)

    from_root = state.resolve_workspace_root(repo)
    from_sub = state.resolve_workspace_root(repo / "sub" / "deeper")
    assert from_root == from_sub == repo.resolve()
    assert state.workspace_key(from_root) == state.workspace_key(from_sub)

    code, job_id = env.dispatch(workspace=repo / "sub" / "deeper")
    assert code == 0
    store = state.JobStore(state.state_root() / state.workspace_key(from_root))
    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    # workspaceRoot is the RESOLVED toplevel: it is what the worker's spec pins
    # bash to, so it must not diverge from the state-dir key.
    assert record["workspaceRoot"] == str(repo.resolve())
    assert record["status"] == state.STATUS_COMPLETED


def test_state_root_resolution(monkeypatch, tmp_path, keyless_home):
    """CLAUDE_PLUGIN_DATA wins; empty or relative values count as unset."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert state.state_root() == tmp_path / "plugin" / "state"

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "   ")
    assert state.state_root() == tmp_path / "xdg" / "chinamax"

    # A relative root would resolve differently in the dispatcher and in the
    # worker, which runs from a different cwd — so it is not a root at all.
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "relative/state")
    assert state.state_root() == tmp_path / "xdg" / "chinamax"

    monkeypatch.setenv("XDG_STATE_HOME", "also/relative")
    assert state.state_root() == keyless_home / ".local" / "state" / "chinamax"

    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    monkeypatch.delenv("XDG_STATE_HOME")
    assert state.state_root() == keyless_home / ".local" / "state" / "chinamax"


def test_permissions(dispatch_env):
    """State dirs are 0700 and every file holding prompt text or results 0600."""
    env = dispatch_env(bash_then_report_script())
    code, job_id = env.dispatch()
    assert code == 0
    store = env.store
    wait_for_status(store, job_id, state.TERMINAL_STATUSES)

    for directory in (store.path, store.jobs_dir, store.steer_dir(job_id)):
        assert os.stat(directory).st_mode & 0o777 == 0o700, directory
    for path in (
        store.index_path,
        store.lock_path,
        store.record_path(job_id),
        store.log_path(job_id),
        store.spawn_log_path(job_id),
        store.transcript_path(job_id),
        # The Runtime writes this one with tmp+rename, so the renamed temporary
        # carries its own mode rather than the precreated 0600.
        store.result_path(job_id),
    ):
        assert os.stat(path).st_mode & 0o777 == 0o600, path


def test_pid_start_time_parsed_with_parens_in_comm():
    """Field 22 is read by splitting after the LAST ')', never by a naive split."""
    assert state.parse_pid_start_time(STAT_WITH_PARENS) == 987654
    assert STAT_WITH_PARENS.split()[21] != "987654"
    assert state.parse_pid_start_time("garbage with no parens") is None
    assert state.read_pid_start_time(os.getpid()) is not None
    # An exited or unreachable process yields null rather than failing a dispatch.
    assert state.read_pid_start_time(2**22 + 7) is None


def test_schema_version_present(dispatch_env):
    """Every record carries schemaVersion."""
    env = dispatch_env([report_turn()])
    code, job_id = env.dispatch()
    assert code == 0
    store = env.store
    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["schemaVersion"] == state.SCHEMA_VERSION == 1
    assert json.loads(store.record_path(job_id).read_text(encoding="utf-8"))[
        "schemaVersion"
    ] == 1


def test_bridge_name_and_session_id_persisted(dispatch_env, monkeypatch):
    """`--bridge-name` round-trips and `sessionId` is populated from CHINAMAX_SESSION_ID."""
    env = dispatch_env([report_turn()])
    # The owning-session field now comes from CHINAMAX_SESSION_ID (the symbolic
    # SESSION_ID_VARIABLE), which the dispatch fixture otherwise clears.
    monkeypatch.setenv(state.SESSION_ID_VARIABLE, "sess-xyz")
    code, job_id = env.dispatch("--bridge-name", "chinamax-deepseek-fix-auth")
    assert code == 0
    record = wait_for_status(env.store, job_id, state.TERMINAL_STATUSES)
    assert record["bridgeName"] == "chinamax-deepseek-fix-auth"
    assert record["sessionId"] == "sess-xyz"


def test_session_id_absent_defaults_none(dispatch_env):
    """With no CHINAMAX_SESSION_ID exported, sessionId and bridgeName default None."""
    env = dispatch_env([report_turn()])
    # dispatch_env already delenv's state.SESSION_ID_VARIABLE.
    code, job_id = env.dispatch()
    assert code == 0
    record = wait_for_status(env.store, job_id, state.TERMINAL_STATUSES)
    assert record["sessionId"] is None
    assert record["bridgeName"] is None


def test_reads_tolerate_additive_change(dispatch_env):
    """An unknown extra field survives a read and a missing optional defaults."""
    env = dispatch_env()
    store = env.store.ensure()
    job_id = make_job(store)
    stored = json.loads(store.record_path(job_id).read_text(encoding="utf-8"))
    stored["unknownFutureField"] = {"added": "by a later plugin"}
    del stored["phase"]
    store.record_path(job_id).write_text(json.dumps(stored), encoding="utf-8")

    record = store.read(job_id)
    assert record["unknownFutureField"] == {"added": "by a later plugin"}
    assert record["phase"] is None
    assert record["status"] == state.STATUS_QUEUED


def test_session_reaper_exists():
    """The session reaper now exists (ADR 0004 reversed): `reap` and its helpers.

    The inverse of the old no-reaper invariant. `reap` is the ONLY verb taking a
    session id; every other verb still rejects `--session`.
    """
    # The reap seam the session hooks call in-process.
    assert callable(state.reap_session)
    assert callable(state.reap_orphans)

    # `reap` parses both forms; a missing target is refused (required group).
    parser = build_parser()
    assert parser.parse_args(["reap", "--session", "sess-1"]).session == "sess-1"
    assert parser.parse_args(["reap", "--orphans"]).orphans is True
    with pytest.raises(SystemExit):
        parser.parse_args(["reap"])

    # No OTHER verb takes a session selector.
    for verb, tail in (
        ("task", ["--profile", PROFILE]),
        ("status", []),
        ("logs", ["task-x-abcdef"]),
    ):
        for flag in ("--session", "--session-id"):
            with pytest.raises(SystemExit):
                main([verb, *tail, flag, "sess-1"])

    # Exhaustive: ONLY `reap` has a "session"-named argument.
    session_owners = set()
    for name, verb in _subparsers(build_parser()).choices.items():
        for action in verb._actions:
            for option in (action.option_strings or [action.dest]):
                if "session" in option.lower():
                    session_owners.add(name)
    assert session_owners == {"reap"}


def test_resolve_job_refuses_a_miss_and_an_ambiguous_prefix(dispatch_env):
    """Exact id beats prefix; a miss or an ambiguity is an error, never silence."""
    env = dispatch_env()
    store = env.store.ensure()

    with pytest.raises(Exception) as empty:
        store.resolve_job("task-nope-000000")
    assert "no Job matching" in str(empty.value)

    first = make_job(store)
    second = make_job(store)
    assert store.resolve_job(first) == first
    assert not second.startswith(first[:-1])
    assert store.resolve_job(first[:-1]) == first
    with pytest.raises(Exception) as ambiguous:
        store.resolve_job("task-")
    assert first in str(ambiguous.value) and second in str(ambiguous.value)


def test_non_linux_process_identity_uses_integer_creation_time(monkeypatch):
    """The portable backend keeps the persisted integer PID identity contract."""
    class FakeProcess:
        pid = 4242

        def ppid(self):
            return 7

        def create_time(self):
            return 123.456789

        def status(self):
            return "running"

        def name(self):
            return "python.exe"

    class FakePsutil:
        STATUS_ZOMBIE = "zombie"
        NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        ZombieProcess = type("ZombieProcess", (Exception,), {})
        AccessDenied = type("AccessDenied", (Exception,), {})

        @staticmethod
        def Process(_pid):
            return FakeProcess()

    monkeypatch.setattr(state.sys, "platform", "win32")
    monkeypatch.setattr(state, "_psutil_module", lambda: FakePsutil)
    assert state.read_pid_start_time(4242) == 123456789
    info = state.read_process(4242)
    assert info is not None
    assert info.start_time == 123456789
    assert state.read_comm(4242) == "python"


def test_non_linux_worker_gone_rejects_pid_reuse(monkeypatch):
    class FakeProcess:
        def status(self):
            return "running"

        def create_time(self):
            return 2.0

    class FakePsutil:
        STATUS_ZOMBIE = "zombie"
        NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        ZombieProcess = type("ZombieProcess", (Exception,), {})
        AccessDenied = type("AccessDenied", (Exception,), {})

        @staticmethod
        def Process(_pid):
            return FakeProcess()

    monkeypatch.setattr(state.sys, "platform", "darwin")
    monkeypatch.setattr(state, "_psutil_module", lambda: FakePsutil)
    assert state.worker_gone({"pid": 4242, "pidStartTime": 1_000_000}) is True


def test_non_linux_access_denied_process_is_alive_unknown(monkeypatch):
    """AccessDenied never becomes a false gone result for lifecycle callers."""
    class FakeProcess:
        def status(self):
            raise FakePsutil.AccessDenied()

    class FakePsutil:
        STATUS_ZOMBIE = "zombie"
        NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        ZombieProcess = type("ZombieProcess", (Exception,), {})
        AccessDenied = type("AccessDenied", (Exception,), {})

        @staticmethod
        def Process(_pid):
            return FakeProcess()

    monkeypatch.setattr(state.sys, "platform", "win32")
    monkeypatch.setattr(state, "_psutil_module", lambda: FakePsutil)
    info = state.read_process(4242)
    assert info == state.ProcessInfo(4242, 0, 0, None, False)
    assert state.worker_gone({"pid": 4242, "pidStartTime": 1}) is False


def test_windows_atomic_replace_retries_and_cleans_temporary(monkeypatch, tmp_path):
    """Windows sharing violations retry five times and failed temps are removed."""
    source = tmp_path / "staged.tmp"
    destination = tmp_path / "published.txt"
    source.write_text("payload", encoding="utf-8")
    calls = []

    def fake_replace(source_name, destination_name):
        calls.append((source_name, destination_name))
        if len(calls) < 3:
            raise PermissionError("sharing violation")
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        source.unlink()

    monkeypatch.setattr(state.sys, "platform", "win32")
    monkeypatch.setattr(state.os, "replace", fake_replace)
    monkeypatch.setattr(state.time, "sleep", lambda _seconds: None)
    state.atomic_replace(source, destination)
    assert len(calls) == 3
    assert destination.read_text(encoding="utf-8") == "payload"
    assert not source.exists()

    source.write_text("discard", encoding="utf-8")

    def always_locked(_source_name, _destination_name):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(state.os, "replace", always_locked)
    with pytest.raises(PermissionError):
        state.atomic_replace(source, destination)
    assert not source.exists()


def test_windows_termination_refuses_unreadable_root(monkeypatch):
    """An unknown root identity is never eligible for destructive signaling."""
    class FakeProcess:
        pid = 4242
        terminate_calls = 0

        def terminate(self):
            self.terminate_calls += 1

    process = FakeProcess()
    monkeypatch.setattr(state.sys, "platform", "win32")
    monkeypatch.setattr(state, "_windows_snapshot", lambda _pid: {4242: (process, None)})
    assert state._windows_identity_holds(process, None) is False
    assert state._terminate_tree_windows(4242, 1_000_000, 0, 0) == [4242]
    assert process.terminate_calls == 0
