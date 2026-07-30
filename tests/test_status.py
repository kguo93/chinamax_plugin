"""`status`: the listing, the bounded `--wait` poll, and the total exit codes."""

from __future__ import annotations

import threading
import time

from chinamax import state
from chinamax.__main__ import main
from chinamax.loop import PHASE_RUNNING_TOOL
from conftest import PROFILE, bash_script, build_record, wait_for, wait_for_status

#: Long enough to observe the Job while it is still running.
SLOW_COMMAND = "sleep 4; echo done"


def running_job(env, phase: str = PHASE_RUNNING_TOOL) -> tuple[object, str]:
    """Build a `running` Job whose only later change is one the test makes.

    Built directly rather than dispatched: `--wait` snapshots at call start, so
    proving it woke on the LOG rather than on a status or phase change means
    holding those two still — which a live worker will not do on cue.
    """
    store = env.store.ensure()
    job_id = store.reserve_id()
    store.create(
        state.new_record(
            job_id,
            prompt="Do the task.",
            profile=PROFILE,
            write=True,
            workspace_root=env.workspace,
            log_file=store.log_path(job_id),
        )
    )
    store.update(
        job_id,
        {"status": state.STATUS_RUNNING, "startedAt": state.utc_now(), "phase": phase},
        expect={state.STATUS_QUEUED},
    )
    log = state.precreate(store.log_path(job_id))
    log.write_text(f"{state.utc_now()} [{phase}] bash: sleeping\n", encoding="utf-8")
    return store, job_id


def append_log_after(path, delay_s: float, text: str) -> threading.Thread:
    """Append one log line after a delay, changing nothing else about the Job."""

    def _append() -> None:
        time.sleep(delay_s)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()

    thread = threading.Thread(target=_append, daemon=True)
    thread.start()
    return thread


def test_running_then_completed_preview(dispatch_env, capsys):
    """status shows phase and a progress preview while running, then completed."""
    env = dispatch_env(bash_script(SLOW_COMMAND))
    code, job_id = env.dispatch()
    assert code == 0
    store = env.store

    # Wait on the LOG, not on the record's phase: record writes are throttled to
    # one per poll interval, so the log is the channel that is never behind.
    assert wait_for(
        lambda: any(
            PHASE_RUNNING_TOOL in line
            for line in state.tail_lines(store.log_path(job_id), 10)
        ),
        60.0,
    )
    assert main(["status", job_id, "--workspace", str(env.workspace)]) == 2
    running = capsys.readouterr().out
    assert job_id in running
    assert state.STATUS_RUNNING in running
    assert PHASE_RUNNING_TOOL in running
    assert "    | " in running, "no progress preview lines"

    wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert main(["status", job_id, "--workspace", str(env.workspace)]) == 0
    finished = capsys.readouterr().out
    assert state.STATUS_COMPLETED in finished

    # The bare listing shows the Job exactly once.
    assert main(["status", "--workspace", str(env.workspace)]) == 0
    assert capsys.readouterr().out.count(job_id) == 1


def test_wait_returns_early(dispatch_env):
    """`--wait` reports completion within ~2 poll intervals of the worker finishing."""
    env = dispatch_env(bash_script(SLOW_COMMAND))
    code, job_id = env.dispatch()
    assert code == 0
    argv = ["status", job_id, "--wait", "--workspace", str(env.workspace)]

    started = time.monotonic()
    while True:
        outcome = main(argv)
        if outcome == 0:
            break
        # Every non-terminal return is 2, including a wake-up on progress.
        assert outcome == 2
        assert time.monotonic() - started < 120, "never reported completion"
    returned_at = time.time()

    record = env.store.read(job_id)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    lag = returned_at - state.parse_timestamp(record["completedAt"])
    assert 0 <= lag <= 3 * state.POLL_INTERVAL_S, f"woke {lag:.1f}s after completion"
    # Well under the bounded window the Bridge polls with.
    assert time.monotonic() - started < state.WAIT_TIMEOUT_MS / 1000.0


def test_wait_wakes_on_log_progress(dispatch_env, capsys):
    """New log lines wake `--wait` even when status and phase never change."""
    env = dispatch_env()
    store, job_id = running_job(env)
    append_log_after(
        store.log_path(job_id), 1.0, f"{state.utc_now()} [{PHASE_RUNNING_TOOL}] bash: ok\n"
    )

    started = time.monotonic()
    outcome = main(["status", job_id, "--wait", "--workspace", str(env.workspace)])
    elapsed = time.monotonic() - started

    assert elapsed < 3 * state.POLL_INTERVAL_S, (
        f"blocked {elapsed:.1f}s: a long single phase went silent, which is exactly "
        "what watching the log's size exists to prevent"
    )
    assert outcome == 2
    record = store.read(job_id)
    assert record["status"] == state.STATUS_RUNNING
    assert record["phase"] == PHASE_RUNNING_TOOL
    assert "bash: ok" in capsys.readouterr().out


def test_progress_return_exits_two(dispatch_env):
    """A progress wake-up exits 2, the same as expiry; only terminal exits 0."""
    env = dispatch_env()
    store, job_id = running_job(env)
    append_log_after(store.log_path(job_id), 1.0, "more progress\n")
    argv = ["status", job_id, "--wait", "--workspace", str(env.workspace)]

    # Woken by progress while still active: 2, never 0 — the Bridge branches on
    # this code, so 0 here would be read as completion mid-run.
    assert main(argv) == 2
    # Expiry at the bound while still active: the same code.
    assert main([*argv, "--timeout-ms", "1"]) == 2

    store.update(
        job_id,
        {"status": state.STATUS_COMPLETED, "completedAt": state.utc_now()},
        expect={state.STATUS_RUNNING},
    )
    assert main(argv) == 0


def test_wait_timeout_is_clamped(dispatch_env, monkeypatch):
    """A `--timeout-ms` past the bound clamps instead of blocking unbounded."""
    env = dispatch_env()
    store, job_id = running_job(env)
    # The bound itself is shrunk so the clamp is observable in a fast test; the
    # assertion is that the CLAMP applies, not what the shipped bound is.
    monkeypatch.setattr(state, "WAIT_TIMEOUT_MS", 1500)

    started = time.monotonic()
    outcome = main(
        [
            "status",
            job_id,
            "--wait",
            "--timeout-ms",
            "99999999",
            "--workspace",
            str(env.workspace),
        ]
    )

    assert outcome == 2
    assert time.monotonic() - started < 10


def test_status_row_is_bridge_first(dispatch_env, capsys):
    """The status row leads with the Bridge name (or '-' for a direct dispatch)."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    named = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_RUNNING,
        bridge_name="chinamax-glm-refactor",
    )
    unnamed = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING)

    assert main(["status", named, "--workspace", workspace]) == 2
    named_out = capsys.readouterr().out
    # The Bridge name leads the row, ahead of the Job id.
    assert "chinamax-glm-refactor" in named_out
    assert named_out.index("chinamax-glm-refactor") < named_out.index(named)

    assert main(["status", unnamed, "--workspace", workspace]) == 2
    unnamed_line = capsys.readouterr().out.splitlines()[0]
    assert unnamed_line.startswith("-  ") and unnamed in unnamed_line


def test_resolution_error_exits_one(dispatch_env, capsys):
    """A named Job that is not found exits 1 listing candidates, never 0."""
    env = dispatch_env()

    # Against an EMPTY store too: exit 0 with no output answers only a bare
    # `status` with nothing to list.
    assert main(["status", "task-nope-000000", "--workspace", str(env.workspace)]) == 1
    assert "no Job matching" in capsys.readouterr().err
    assert main(["status", "--workspace", str(env.workspace)]) == 0
    assert capsys.readouterr().out == ""

    running_job(env)
    assert main(["logs", "task-nope-000000", "--workspace", str(env.workspace)]) == 1
    assert "no Job matching" in capsys.readouterr().err
