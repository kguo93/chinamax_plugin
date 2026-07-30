"""`cancel`: the whole process TREE dies, and the record is never clobbered.

Also `reap` — the session-scoped kill/mark verb (ADR 0004 reversed) that reuses
cancel's `terminate_tree` discipline.
"""

from __future__ import annotations

import os

from chinamax import state
from chinamax.__main__ import CANCEL_REASON, main
from conftest import (
    REPORT_PAYLOAD,
    bash_script,
    build_record,
    dead_pid,
    wait_for,
    wait_for_status,
)

#: Long enough that the bash tool's child is demonstrably still running.
CHILD_COMMAND = "sleep 300"


def own_session_children(worker: int) -> list[int]:
    """Return the worker's descendants living OUTSIDE the worker's own group.

    These are what the bash tool's ``start_new_session=True`` produces, and they
    are the point of the tree test: a single ``killpg`` on the worker's group
    leaves every one of them running.
    """
    tree = state.descendants(worker)
    rooted = tree.get(worker)
    if rooted is None:
        return []
    return sorted(pid for pid, info in tree.items() if info.pgid != rooted.pgid)


def alive(pid: int) -> bool:
    """Report whether a pid is still a running process (a zombie is not)."""
    info = state.read_process(pid)
    return info is not None and not info.zombie


def test_cancel_kills_tree(dispatch_env, capsys):
    """Cancel stops the worker AND the child it started in its own session."""
    env = dispatch_env(bash_script(CHILD_COMMAND))
    code, job_id = env.dispatch()
    assert code == 0
    store = env.store

    record = wait_for_status(store, job_id, {state.STATUS_RUNNING})
    worker = record["pid"]
    assert isinstance(worker, int), record
    assert wait_for(lambda: own_session_children(worker), 60.0), (
        "the bash tool's child never appeared; the tree test would be vacuous"
    )
    children = own_session_children(worker)

    assert main(["cancel", job_id, "--workspace", str(env.workspace)]) == 0

    assert not alive(worker), "the worker survived its own cancel"
    for child in children:
        assert not alive(child), f"{child} survived: the group is not the tree"
    assert state.STATUS_CANCELLED in capsys.readouterr().out

    cancelled = store.read(job_id)
    assert cancelled["status"] == state.STATUS_CANCELLED
    assert cancelled["errorMessage"] == CANCEL_REASON
    assert cancelled["completedAt"] is not None


def test_ambiguous_cancel_refuses(dispatch_env, capsys):
    """With two active Jobs and no id, cancel refuses and lists both."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    first = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING)
    second = build_record(store, workspace=env.workspace, status=state.STATUS_QUEUED)

    assert main(["cancel", "--workspace", workspace]) == 1
    refusal = capsys.readouterr().err
    assert first in refusal and second in refusal
    assert store.read(first)["status"] == state.STATUS_RUNNING
    assert store.read(second)["status"] == state.STATUS_QUEUED

    # Named explicitly, each is cancellable; with none left active, a bare
    # cancel says so rather than picking a finished Job.
    for job_id in (first, second):
        assert main(["cancel", job_id, "--workspace", workspace]) == 0
        assert store.read(job_id)["status"] == state.STATUS_CANCELLED
    capsys.readouterr()
    assert main(["cancel", "--workspace", workspace]) == 1
    assert "no active Job" in capsys.readouterr().err


def test_cancel_does_not_clobber_completed_result(dispatch_env, monkeypatch, capsys):
    """A Job that completes DURING the kill keeps its result and its status.

    The barrier is a real one — the store, the compare-and-swap and the terminal
    write are all genuine; only the MOMENT the competing write lands is pinned,
    because a live worker will not race on cue.
    """
    env = dispatch_env()
    store = env.store
    job_id = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_RUNNING,
        pid=dead_pid(),
    )
    real_terminate = state.terminate_tree

    def complete_during_the_kill(pid, start_time=None, grace_s=state.TERMINATE_GRACE_S):
        survivors = real_terminate(pid, start_time, grace_s)
        store.update(
            job_id,
            {
                "status": state.STATUS_COMPLETED,
                "result": REPORT_PAYLOAD,
                "completedAt": state.utc_now(),
            },
            expect={state.STATUS_RUNNING},
        )
        return survivors

    monkeypatch.setattr(state, "terminate_tree", complete_during_the_kill)

    assert main(["cancel", job_id, "--workspace", str(env.workspace)]) == 0

    assert state.STATUS_COMPLETED in capsys.readouterr().out
    kept = store.read(job_id)
    assert kept["status"] == state.STATUS_COMPLETED
    assert kept["result"] == REPORT_PAYLOAD
    assert kept["errorMessage"] is None


def test_reap_session_cancels_owner_only(dispatch_env, capsys):
    """`reap --session` marks that session's stored-active Jobs cancelled; other
    sessions and already-terminal Jobs are untouched."""
    env = dispatch_env()
    store = env.store
    mine = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING,
        session_id="S1", bridge_name="chinamax-glm-mine",
    )
    theirs = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING, session_id="S2",
    )
    finished = build_record(
        store, workspace=env.workspace, status=state.STATUS_COMPLETED,
        completed_at=state.utc_now(), session_id="S1",
    )

    assert main(["reap", "--session", "S1"]) == 0
    # One bridge-first digest line for the reaped Job.
    assert mine in capsys.readouterr().out
    assert store.read(mine)["status"] == state.STATUS_CANCELLED
    assert store.read(theirs)["status"] == state.STATUS_RUNNING
    assert store.read(finished)["status"] == state.STATUS_COMPLETED


def test_reap_orphans_interrupts_dead_sessions(dispatch_env, capsys):
    """`reap --orphans` interrupts a dead-session Job, spares a live session, and
    reports (never kills) an ownerless live worker; stale registries are GC'd."""
    env = dispatch_env()
    store = env.store
    dead = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING, session_id="DEAD",
    )
    state.write_session_registry("DEAD", dead_pid(), None)
    live = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING, session_id="LIVE",
    )
    state.write_session_registry("LIVE", os.getpid(), state.read_pid_start_time(os.getpid()))
    # No sessionId and no pid -> never gone -> reported, never killed.
    ownerless = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING)

    assert main(["reap", "--orphans"]) == 0
    err = capsys.readouterr().err
    assert store.read(dead)["status"] == state.STATUS_INTERRUPTED
    assert store.read(live)["status"] == state.STATUS_RUNNING
    assert store.read(ownerless)["status"] == state.STATUS_RUNNING
    assert ownerless in err
    assert state.read_session_registry("DEAD") is None
    assert state.read_session_registry("LIVE") is not None
