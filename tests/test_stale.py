"""Crash detection: a dead worker reads as `interrupted`, and its Thread survives."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass

import pytest

from chinamax import state
from chinamax.__main__ import main
from conftest import (
    PROFILE,
    aged,
    assert_wire_shape,
    build_record,
    dead_pid,
    report_turn,
    wait_for,
    wait_for_status,
)
from fake_provider import hang_fault

FOLLOW_UP = "Pick this back up."
#: A half-written JSONL append: the shape a crash mid-flush leaves behind.
TORN_LINE = '{"v": 1, "ts": "2026-07-23T00:00:00+00:00", "kind": "mess'


def message_line(role: str, content: list[dict]) -> str:
    """Render one transcript ``message`` record, as the Runtime writes it."""
    return (
        json.dumps(
            {
                "v": 1,
                "ts": "2026-07-23T00:00:00+00:00",
                "kind": "message",
                "role": role,
                "content": content,
            }
        )
        + "\n"
    )


@dataclass(frozen=True)
class Tail:
    """One transcript tail shape and the message roles it must normalize to."""

    lines: list[str]
    roles: list[str]


TAILS = {
    # (1) A trailing line that does not parse is a half-written append, dropped.
    "torn-final-line": Tail(
        lines=[
            message_line("user", [{"type": "text", "text": "Do the task."}]),
            message_line("assistant", [{"type": "text", "text": "Looking now."}]),
            TORN_LINE,
        ],
        roles=["user", "assistant", "user"],
    ),
    # (2) An unanswered tool_use is ANSWERED, and its turn is preserved.
    "unanswered-tool-use": Tail(
        lines=[
            message_line("user", [{"type": "text", "text": "Do the task."}]),
            message_line(
                "assistant",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_torn_1",
                        "name": "bash",
                        "input": {"command": "ls"},
                    }
                ],
            ),
        ],
        roles=["user", "assistant", "user"],
    ),
    # (3) A trailing user turn takes the follow-up as another text block, never
    # a second consecutive user message.
    "trailing-user-turn": Tail(
        lines=[message_line("user", [{"type": "text", "text": "Do the task."}])],
        roles=["user"],
    ),
}


def test_sigkilled_worker_reported_interrupted(dispatch_env, capsys):
    """A SIGKILLed worker reads as interrupted, and its Thread stays resumable."""
    env = dispatch_env([hang_fault()])
    hanging = env.providers[PROFILE]
    code, job_id = env.dispatch()
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    record = wait_for_status(store, job_id, {state.STATUS_RUNNING})
    worker = record["pid"]
    assert isinstance(worker, int), record

    # Kill it INSIDE the API call: the transcript is write-ahead, so by then the
    # outgoing user turn is on disk and unanswered — the tail this test is about.
    assert wait_for(lambda: hanging.requests, 60.0)
    os.kill(worker, signal.SIGKILL)
    assert wait_for(lambda: state.worker_gone(store.read(job_id)), 30.0), (
        "a SIGKILLed worker must read as gone; a zombie still answers signal 0"
    )
    # In production the worker is init's child and is reaped the instant it
    # dies; here pytest is its parent, so the zombie is cleared by hand.
    with contextlib.suppress(ChildProcessError):
        os.waitpid(worker, 0)

    # Age the heartbeat past the grace rather than sleeping it out.
    store.update(job_id, {"updatedAt": aged(state.STALE_GRACE_S * 2)}, touch=False)
    stale = store.read(job_id)
    assert stale["status"] == state.STATUS_RUNNING, "detection is READ-side only"
    assert state.effective_status(stale) == state.STATUS_INTERRUPTED
    assert not state.is_active(stale)

    assert main(["status", job_id, "--workspace", workspace]) == 0
    assert state.STATUS_INTERRUPTED in capsys.readouterr().out

    # --wait returns at once with 0: an interrupted Job never leaves this state,
    # so a poll-relay that kept waiting would hang for the Bridge's whole life.
    started = time.monotonic()
    assert main(["status", job_id, "--wait", "--workspace", workspace]) == 0
    assert time.monotonic() - started < state.POLL_INTERVAL_S
    capsys.readouterr()

    # Not active, so the Thread resumes even though the record still says running.
    provider = env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, job_id, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    continued = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert continued["status"] == state.STATUS_COMPLETED, continued.get("errorMessage")
    sent = provider.requests[0]["body"]["messages"]
    assert_wire_shape(sent)
    # The write-ahead delta of the call the SIGKILL landed during is a trailing
    # USER turn, so the follow-up joins it instead of doubling the role.
    assert [message["role"] for message in sent] == ["user"]
    assert FOLLOW_UP in json.dumps(sent)


def test_queued_job_with_dead_pid_is_interrupted(dispatch_env, capsys):
    """A queued record whose recorded pid is dead grades as interrupted.

    This is the worker-died-before-claiming shape the dispatcher's immediate pid
    write hands to this detector; grading only `running` strands it active
    forever.
    """
    env = dispatch_env()
    store = env.store
    job_id = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_QUEUED,
        pid=dead_pid(),
    )

    # Conjunctive: a dead worker with a FRESH heartbeat is not yet interrupted.
    assert state.effective_status(store.read(job_id)) == state.STATUS_QUEUED

    store.update(job_id, {"updatedAt": aged(state.STALE_GRACE_S * 2)}, touch=False)
    stale = store.read(job_id)
    assert stale["status"] == state.STATUS_QUEUED
    assert state.effective_status(stale) == state.STATUS_INTERRUPTED
    assert not state.is_active(stale)

    assert main(["status", job_id, "--workspace", str(env.workspace)]) == 0
    assert state.STATUS_INTERRUPTED in capsys.readouterr().out


@pytest.mark.parametrize("shape", list(TAILS), ids=list(TAILS))
def test_resume_normalizes_torn_transcript(dispatch_env, capsys, shape):
    """Every crash-shaped tail normalizes into a history the provider accepts."""
    tail = TAILS[shape]
    env = dispatch_env()
    store = env.store
    source = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_COMPLETED,
        completed_at=state.utc_now(),
    )
    state.precreate(store.transcript_path(source)).write_text(
        "".join(tail.lines), encoding="utf-8"
    )

    provider = env.bind([report_turn()])
    assert main(["resume", "--workspace", str(env.workspace), source, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    sent = provider.requests[0]["body"]["messages"]
    assert_wire_shape(sent)
    assert [message["role"] for message in sent] == tail.roles
    assert sent[-1]["role"] == "user"
    assert FOLLOW_UP in json.dumps(sent[-1])
    # Repaired, not truncated: the source's own turns are all still there. The
    # role list is what proves the torn line was dropped rather than replayed —
    # a fourth message would appear the moment it was parsed as a turn.
    assert "Do the task." in json.dumps(sent[0])
