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


def test_no_session_reaper():
    """No session-boundary kill path exists in this slice's public surface.

    The manifest half of the invariant — hooks.json registering no SessionEnd
    entry — is asserted by surface/02, where the manifest actually exists.
    """
    reaping = [
        name
        for name in dir(state) + dir(state.JobStore)
        if not name.startswith("_")
        and any(word in name.lower() for word in ("reap", "delete", "purge", "destroy", "kill"))
    ]
    assert reaping == []

    for verb, tail in (
        ("task", ["--profile", PROFILE]),
        ("status", []),
        ("logs", ["task-x-abcdef"]),
    ):
        for flag in ("--session", "--session-id"):
            with pytest.raises(SystemExit):
                main([verb, *tail, flag, "sess-1"])

    # Exhaustive rather than a spot check: every argument of every verb.
    named = [
        name
        for verb in _subparsers(build_parser()).choices.values()
        for action in verb._actions
        for name in (action.option_strings or [action.dest])
    ]
    assert [name for name in named if "session" in name.lower()] == []


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
