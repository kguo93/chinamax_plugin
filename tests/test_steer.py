"""The steer queue: a durable mid-run message injected at the next loop boundary.

These tests drive the real detached worker against the fake provider. The turn
gate holds the worker between two scripted turns so a steer can be written into
that window deterministically; the drain abort point reproduces a crash between
the transcript flush and the rename; the steer clock override pins two writers to
one millisecond.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime

from chinamax import state
from chinamax.__main__ import main
from chinamax.loop import STEER_ABORT_VARIABLE
from chinamax.transcript import STEER_MARKER
from conftest import (
    aged,
    assert_wire_shape,
    bash_then_report_script,
    build_record,
    dead_pid,
    report_turn,
    wait_for_status,
)

STEER_MSG = "stop touching module X"


def steer(env, job_id, message, workspace=None) -> tuple[int, str, str]:
    """Invoke the `steer` verb in-process; return ``(code, stdout, stderr)``.

    ``--workspace`` leads, mirroring `resume`'s CLI shape, so the single ``args``
    positional collects the id and the post-``--`` message across it. ``job_id``
    of None is a BARE steer that resolves the workspace's single active Job.
    """
    ws = str(env.workspace if workspace is None else workspace)
    out, err = io.StringIO(), io.StringIO()
    argv = ["steer", "--workspace", ws]
    if job_id is not None:
        argv.append(job_id)
    if message is not None:
        argv += ["--", message]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue().strip(), err.getvalue().strip()


def steer_id_of(stdout: str) -> str:
    """Pull the steer id off a successful `steer` line (`… steer queued  <id>`)."""
    return stdout.split()[-1]


def gated_dispatch(env, script, gate_index):
    """Dispatch, hold the worker at ``gate_index``, and return the release handle.

    Returns ``(provider, job_id, release)``; the worker is blocked having just
    sent the gated turn's request, so the caller can write into the window and
    then set ``release``.
    """
    provider = env.bind(script)
    reached, release = provider.gate(gate_index)
    code, job_id = env.dispatch()
    assert code == 0, "dispatch should return the id immediately"
    assert reached.wait(60.0), "worker never reached the gated turn"
    return provider, job_id, release


def last_user_texts(request: dict) -> list[str]:
    """Return the text blocks of a recorded request's final user turn."""
    messages = request["body"]["messages"]
    assert messages[-1]["role"] == "user"
    return [
        block["text"]
        for block in messages[-1]["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    ]


def queued_steer_files(store, job_id: str) -> list[str]:
    """Return the names of steers still sitting in the queue (undrained)."""
    return [path.name for path in state.list_steer_files(store.steer_dir(job_id))]


def consumed_steer_files(store, job_id: str) -> list[str]:
    """Return the names of steers moved into ``consumed/`` after draining."""
    consumed = store.steer_dir(job_id) / state.STEER_CONSUMED_DIRNAME
    if not consumed.is_dir():
        return []
    return sorted(entry.name for entry in os.scandir(consumed) if entry.name.endswith(".md"))


def test_steer_lands_next_turn(dispatch_env):
    """A steer written between turns rides the very next request's user turn.

    Held at the first turn, the steer is written and released; the drain at the
    top of the next iteration folds it onto the outgoing user message — a SINGLE
    user turn carrying that turn's tool_result AND the steer, not two consecutive
    user messages (issue AC bullet 1).
    """
    env = dispatch_env()
    provider, job_id, release = gated_dispatch(env, bash_then_report_script(), 0)
    store = env.store

    code, out, err = steer(env, job_id, STEER_MSG)
    assert code == 0, err
    assert store.read(job_id)["status"] == state.STATUS_RUNNING
    release.set()

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    # The steer landed in the turn AFTER the one it was written during.
    carrying = provider.requests[1]
    assert STEER_MARKER + STEER_MSG in last_user_texts(carrying)
    sent = carrying["body"]["messages"]
    assert_wire_shape(sent)  # no two consecutive user messages
    # The tool_result and the steer share one user turn — the drain rode the
    # steer onto the message already carrying that turn's tool_result.
    assert sent[-1]["content"][0].get("type") == "tool_result"


def test_order_and_exactly_once(dispatch_env):
    """Multiple steers inject in write order, exactly once each (AC bullet 2)."""
    env = dispatch_env()
    provider, job_id, release = gated_dispatch(env, bash_then_report_script(), 0)
    store = env.store

    messages = ["first steer", "second steer", "third steer"]
    ids = []
    for message in messages:
        code, out, err = steer(env, job_id, message)
        assert code == 0, err
        ids.append(steer_id_of(out))
    release.set()

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    texts = last_user_texts(provider.requests[1])
    injected = [text for text in texts if text.startswith(STEER_MARKER)]
    assert injected == [STEER_MARKER + message for message in messages], "wrong order"

    # Exactly once each: the marked text appears once across every request.
    whole = json.dumps([request["body"]["messages"] for request in provider.requests])
    for message in messages:
        assert whole.count(STEER_MARKER + message) == 1, message
    # All three acknowledged into consumed/, none left queued.
    assert sorted(consumed_steer_files(store, job_id)) == sorted(ids)
    assert queued_steer_files(store, job_id) == []


def test_survives_restart_between_write_and_drain(dispatch_env):
    """A steer queued for a dead worker is injected by the relaunched one.

    The Job's worker is confirmed gone, then `task-worker` is relaunched on the
    SAME Job — admitted by the reclaim arm, since `resume` would build a NEW Job
    with its own empty queue and never see this file. The drain injects the steer
    before the first API call (AC bullet 2).
    """
    env = dispatch_env()
    provider = env.bind([report_turn()])
    store = env.store
    job_id = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_RUNNING,
        pid=dead_pid(),  # a confirmed-dead worker: the reclaim's precondition
    )
    # A one-turn Thread to seed from, and a steer queued directly (the verb would
    # be a red herring here — the point is a file that outlived its writer).
    state.precreate(store.transcript_path(job_id)).write_text(
        json.dumps(
            {
                "v": 1,
                "ts": "2026-07-23T00:00:00+00:00",
                "kind": "message",
                "role": "user",
                "content": [{"type": "text", "text": "Do the task."}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state.write_steer(store.steer_dir(job_id), STEER_MSG)

    assert main(["task-worker", "--job-id", job_id, "--state-dir", str(store.path)]) == 0
    assert store.read(job_id)["status"] == state.STATUS_COMPLETED

    sent = provider.requests[0]["body"]["messages"]
    assert_wire_shape(sent)
    # Injected before the first (and only) API call, folded onto the seeded turn.
    assert [message["role"] for message in sent] == ["user"]
    assert STEER_MARKER + STEER_MSG in json.dumps(sent)
    assert consumed_steer_files(store, job_id), "the steer should be acknowledged"


def test_crash_mid_drain_injects_once(dispatch_env, tmp_path):
    """A crash between the flush and the rename still yields exactly one delivery.

    The abort point kills the worker with the steer flushed to the Thread but
    still queued; the relaunched worker seeds from the Thread and, finding the id
    already recorded, moves the file to consumed/ WITHOUT re-injecting. The steer
    reaches the provider exactly once across both runs — the failure this guards
    is ZERO deliveries, where a mis-seeded relaunch lets the dedupe swallow a
    steer that never reached the provider (AC bullet 2).
    """
    env = dispatch_env()
    provider = env.bind([report_turn()])
    store = env.store
    job_id = build_record(store, workspace=env.workspace, status=state.STATUS_QUEUED)
    state.precreate(store.transcript_path(job_id))
    steer_name = state.write_steer(store.steer_dir(job_id), STEER_MSG)

    # Run 1: a real subprocess that hard-exits in the flush→rename window.
    aborting = os.environ.copy()
    aborting[STEER_ABORT_VARIABLE] = "1"
    first = subprocess.run(
        [sys.executable, "-m", "chinamax", "task-worker",
         "--job-id", job_id, "--state-dir", str(store.path)],
        env=aborting, capture_output=True, text=True, timeout=60,
    )
    assert first.returncode == 137, first.stderr
    assert provider.requests == [], "run 1 aborted before any API call"
    # The steer is flushed to the Thread but still queued: the crash window.
    assert steer_name in {record.get("steer_id") for record in _records(store, job_id)}
    assert steer_name in queued_steer_files(store, job_id)

    # Run 2: relaunch (reclaim), seed from the Thread, dedupe-skip, and finish.
    assert main(["task-worker", "--job-id", job_id, "--state-dir", str(store.path)]) == 0
    assert store.read(job_id)["status"] == state.STATUS_COMPLETED

    # Exactly once across BOTH runs — and the seeded steer DID reach the provider.
    assert len(provider.requests) == 1
    sent = provider.requests[0]["body"]["messages"]
    assert json.dumps(sent).count(STEER_MARKER + STEER_MSG) == 1
    assert_wire_shape(sent)
    assert steer_name in consumed_steer_files(store, job_id)
    assert queued_steer_files(store, job_id) == []


def test_same_millisecond_writers_both_survive(dispatch_env):
    """Two writers pinned to one epoch-ms both land and both inject.

    The clock override forces both `steer` processes into a single millisecond,
    so the random suffix — not the millisecond — is what keeps their names
    distinct; neither overwrites the other.
    """
    env = dispatch_env()
    provider, job_id, release = gated_dispatch(env, bash_then_report_script(), 0)
    store = env.store

    pinned = os.environ.copy()
    pinned[state.STEER_CLOCK_VARIABLE] = "1721000000000"
    ids = []
    for message in ("alpha steer", "beta steer"):
        finished = subprocess.run(
            [sys.executable, "-m", "chinamax", "steer",
             "--workspace", str(env.workspace), job_id, "--", message],
            env=pinned, capture_output=True, text=True, timeout=30,
        )
        assert finished.returncode == 0, finished.stderr
        ids.append(steer_id_of(finished.stdout.strip()))
    # Distinct names despite the same millisecond and the same per-process seq.
    assert len(set(ids)) == 2
    assert all(steer_id.startswith("1721000000000-0000-") for steer_id in ids)
    release.set()

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    texts = last_user_texts(provider.requests[1])
    assert STEER_MARKER + "alpha steer" in texts
    assert STEER_MARKER + "beta steer" in texts
    assert sorted(consumed_steer_files(store, job_id)) == sorted(ids)


def test_transcript_and_log_stamped(dispatch_env):
    """The drained steer is stamped in both the Thread and the log (AC bullet 3)."""
    env = dispatch_env()
    provider, job_id, release = gated_dispatch(env, bash_then_report_script(), 0)
    store = env.store

    code, out, err = steer(env, job_id, STEER_MSG)
    assert code == 0, err
    steer_name = steer_id_of(out)
    release.set()
    wait_for_status(store, job_id, state.TERMINAL_STATUSES)

    # The transcript's steer entry carries the id and enqueue timestamp as
    # metadata FIELDS, and the marked text as replayable content.
    stamped = [
        record for record in _records(store, job_id) if record.get("steer_id") == steer_name
    ]
    assert len(stamped) == 1, "exactly one transcript steer entry"
    entry = stamped[0]
    assert entry["kind"] == "message" and entry["role"] == "user"
    assert entry["content"][0]["text"] == STEER_MARKER + STEER_MSG
    datetime.fromisoformat(entry["steer_ts"])  # a real timestamp, parses

    # The log line carries a leading timestamp and the steer id.
    log_lines = [
        line
        for line in store.log_path(job_id).read_text(encoding="utf-8").splitlines()
        if steer_name in line
    ]
    assert log_lines, "the drain must log the steer"
    for line in log_lines:
        datetime.fromisoformat(line.split(" ", 1)[0])  # the leading timestamp parses


def test_finished_refused_unknown_errors(dispatch_env):
    """Terminal and interrupted Jobs refuse to resume; bad input errors (bullet 4)."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)

    # Each of the three terminal states refuses, naming resume.
    for status in (state.STATUS_COMPLETED, state.STATUS_FAILED, state.STATUS_CANCELLED):
        job_id = build_record(
            store, workspace=env.workspace, status=status, completed_at=state.utc_now()
        )
        code, out, err = steer(env, job_id, STEER_MSG)
        assert code == 1, status
        assert "resume" in err and job_id in err
        assert queued_steer_files(store, job_id) == [], "a refusal writes no file"

    # An effectively-interrupted Job (dead worker, aged heartbeat) also refuses.
    interrupted = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING, pid=dead_pid()
    )
    store.update(interrupted, {"updatedAt": aged(state.STALE_GRACE_S * 2)}, touch=False)
    assert state.effective_status(store.read(interrupted)) == state.STATUS_INTERRUPTED
    code, out, err = steer(env, interrupted, STEER_MSG)
    assert code == 1
    assert "resume" in err and queued_steer_files(store, interrupted) == []

    # Unknown id and an ambiguous prefix both exit 1 with a clear error.
    assert steer(env, "task-nope-000000", STEER_MSG)[0] == 1
    _, _, unknown_err = steer(env, "task-nope-000000", STEER_MSG)
    assert "no Job matching" in unknown_err
    first = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING,
                         pid=os.getpid())
    second = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING,
                          pid=os.getpid())
    prefix = os.path.commonprefix([first, second])
    _, _, ambiguous_err = steer(env, prefix, STEER_MSG)
    assert "ambiguous" in ambiguous_err

    # An empty message exits non-zero and writes nothing, even to a steerable Job.
    steerable = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING, pid=os.getpid()
    )
    assert state.effective_status(store.read(steerable)) == state.STATUS_RUNNING
    code, out, err = steer(env, steerable, "   ")
    assert code == 1
    assert queued_steer_files(store, steerable) == []


def test_bare_steer_targets_single_active(dispatch_env):
    """With no id a bare steer targets the workspace's single active Job.

    The `/chinamax:steer` command form (decision 6) mirrors `cancel`: an id is
    optional, and the lone active Job is resolved rather than guessed.
    """
    env = dispatch_env()
    provider, job_id, release = gated_dispatch(env, bash_then_report_script(), 0)
    store = env.store

    code, out, err = steer(env, None, STEER_MSG)
    assert code == 0, err
    assert job_id in out, "the queued line names the resolved active Job"
    release.set()

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    assert STEER_MARKER + STEER_MSG in json.dumps(provider.requests[1]["body"]["messages"])


def test_bare_steer_lists_several_active(dispatch_env):
    """Several active Jobs make a bare steer a refusal listing them, not a guess."""
    env = dispatch_env()
    store = env.store
    first = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING,
                         pid=os.getpid())
    second = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING,
                          pid=os.getpid())

    code, out, err = steer(env, None, STEER_MSG)
    assert code == 1
    assert "several" in err.lower()
    assert first in err and second in err
    # A refusal writes no file to either Job.
    assert queued_steer_files(store, first) == []
    assert queued_steer_files(store, second) == []


def test_bare_steer_with_nothing_active_refused(dispatch_env):
    """A bare steer with no active Job is a clean refusal, never a crash."""
    env = dispatch_env()
    code, out, err = steer(env, None, STEER_MSG)
    assert code == 1
    assert "no active Job" in err


def test_undelivered_steer_swept_at_exit(dispatch_env):
    """A steer that loses the accept-versus-terminate race leaves a trace.

    Held at the FINAL turn, the steer is written after that turn's drain has
    already run; the Job completes without draining it, and the terminal sweep
    logs its id as undelivered rather than letting it vanish silently.
    """
    env = dispatch_env()
    provider, job_id, release = gated_dispatch(env, bash_then_report_script(), 1)
    store = env.store

    code, out, err = steer(env, job_id, STEER_MSG)
    assert code == 0, err
    steer_name = steer_id_of(out)
    release.set()

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    # Never drained, so never injected and never consumed — but auditable.
    whole = json.dumps([request["body"]["messages"] for request in provider.requests])
    assert STEER_MARKER + STEER_MSG not in whole
    assert steer_name in queued_steer_files(store, job_id)
    log = store.log_path(job_id).read_text(encoding="utf-8")
    assert any(
        steer_name in line and "undelivered" in line for line in log.splitlines()
    ), "the terminal sweep must name the undelivered steer"


def _records(store, job_id: str) -> list[dict]:
    """Return the parsed transcript records of a Job (skipping blank lines)."""
    text = store.transcript_path(job_id).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]
