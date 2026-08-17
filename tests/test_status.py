"""`status`: the listing, the bounded `--wait` poll, and the total exit codes."""

from __future__ import annotations

import threading
import time

from chinamax import state
from chinamax.__main__ import main
from chinamax.loop import PHASE_RUNNING_TOOL
from conftest import (
    PROFILE,
    bash_script,
    build_record,
    wait_for,
    wait_for_status,
    write_policy_settings,
)

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


def test_wait_stamps_supervision(dispatch_env):
    """`status --wait` on an active Job stamps the supervision heartbeat.

    The stamp records ``supervisedAt`` + the clamped bound WITHOUT refreshing
    ``updatedAt`` (``touch=False``, so the 60 s crash grace is untouched); a
    later poll with a smaller bound never lowers the stored one; ``--timeout-ms
    0`` and a terminal Job stamp nothing.
    """
    env = dispatch_env()
    store, job_id = running_job(env)
    workspace = str(env.workspace)
    updated_before = store.read(job_id)["updatedAt"]

    # --timeout-ms 0: the poll returns at once and stamps NOTHING.
    assert main(["status", job_id, "--wait", "--timeout-ms", "0", "--workspace", workspace]) == 2
    zero = store.read(job_id)
    assert zero["supervisedAt"] is None
    assert zero["supervisionTimeoutMs"] is None

    # A real bound stamps supervisedAt + the clamped bound; updatedAt is untouched
    # (a default touch would re-mask a crashed worker for the whole 60 s grace).
    assert main(["status", job_id, "--wait", "--timeout-ms", "1000", "--workspace", workspace]) == 2
    stamped = store.read(job_id)
    assert stamped["supervisedAt"] is not None
    assert stamped["supervisionTimeoutMs"] == 1000
    assert stamped["updatedAt"] == updated_before

    # A smaller later bound never lowers the stored one — an operator status --wait
    # must not shrink the Bridge's supervision threshold under its own poll.
    assert main(["status", job_id, "--wait", "--timeout-ms", "500", "--workspace", workspace]) == 2
    assert store.read(job_id)["supervisionTimeoutMs"] == 1000

    # A terminal Job gets no stamp: clear the fields, mark completed, re-wait.
    store.update(
        job_id,
        {
            "status": state.STATUS_COMPLETED,
            "completedAt": state.utc_now(),
            "supervisedAt": None,
            "supervisionTimeoutMs": None,
        },
        expect={state.STATUS_RUNNING},
    )
    assert main(["status", job_id, "--wait", "--timeout-ms", "1000", "--workspace", workspace]) == 0
    terminal = store.read(job_id)
    assert terminal["supervisedAt"] is None
    assert terminal["supervisionTimeoutMs"] is None


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


def test_status_shows_pinned_model_in_row_and_detail(dispatch_env, capsys):
    """A pinned model shows in the row's profile cell, and `status <id>` adds a
    `model:` detail line; an unpinned Job shows neither, and a bare listing shows
    the pin once — the row cell only, never a per-row detail line."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    pinned = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING, model="custom-m"
    )
    unpinned = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING)

    assert main(["status", pinned, "--workspace", workspace]) == 2
    pinned_out = capsys.readouterr().out
    assert f"{PROFILE} (custom-m)" in pinned_out
    assert "    model: custom-m" in pinned_out

    assert main(["status", unpinned, "--workspace", workspace]) == 2
    unpinned_out = capsys.readouterr().out
    assert "custom-m" not in unpinned_out
    assert "model:" not in unpinned_out

    # A bare listing shows the pin once — the row cell only, no detail line.
    assert main(["status", "--workspace", workspace]) == 0
    listing = capsys.readouterr().out
    assert listing.count("custom-m") == 1
    assert "model:" not in listing


def test_status_escapes_control_char_in_pinned_model(dispatch_env, capsys):
    """An operator-supplied pin with a control character renders escaped in BOTH
    the row cell and the `model:` detail line — one row is always one line."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    pinned = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING, model="custom-\x1bm"
    )

    assert main(["status", pinned, "--workspace", workspace]) == 2
    out = capsys.readouterr().out
    assert "\x1b" not in out, "a raw control character escaped into the rendering"
    assert out.count("custom-\\x1bm") == 2, "escaped in the row cell AND the detail line"


def test_status_footer_reports_policy_toggles(dispatch_env, capsys):
    """The bare-listing footer reports the toggle values and flags a malformed file."""
    env = dispatch_env()
    write_policy_settings(memory=True, hooks=False, mcp=True)

    assert main(["status", "--workspace", str(env.workspace)]) == 0
    listing = capsys.readouterr().out
    assert "policy: memory on  hooks off  mcp on" in listing
    assert "policy settings: " in listing

    # A malformed settings.json flags in the footer but never breaks the listing.
    (state.state_root() / "settings.json").write_text("{bad", encoding="utf-8")
    assert main(["status", "--workspace", str(env.workspace)]) == 0
    assert "MALFORMED" in capsys.readouterr().out


def test_resolution_error_exits_one(dispatch_env, capsys):
    """A named Job that is not found exits 1 listing candidates, never 0."""
    env = dispatch_env()

    # Against an EMPTY store too: a bare `status` with nothing to list exits 0
    # and prints only the policy footer (no Job rows).
    assert main(["status", "task-nope-000000", "--workspace", str(env.workspace)]) == 1
    assert "no Job matching" in capsys.readouterr().err
    assert main(["status", "--workspace", str(env.workspace)]) == 0
    listing = capsys.readouterr().out
    assert "policy: memory off  hooks off  mcp off" in listing
    assert "policy settings: " in listing and "settings.json" in listing

    running_job(env)
    assert main(["logs", "task-nope-000000", "--workspace", str(env.workspace)]) == 1
    assert "no Job matching" in capsys.readouterr().err
