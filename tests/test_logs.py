"""`logs`: the progress log, and the spawn log when there is no progress at all."""

from __future__ import annotations

import pytest

from chinamax import state
from chinamax.__main__ import main
from chinamax.loop import PHASE_RUNNING_TOOL
from conftest import PROFILE, bash_then_report_script, wait_for_status

SPAWN_OUTPUT = "Traceback (most recent call last):\nModuleNotFoundError: no chinamax\n"


def make_record(env, status: str) -> tuple[object, str]:
    """Publish one record with a spawn log and no progress log at all."""
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
    if status != state.STATUS_QUEUED:
        store.update(job_id, {"status": status}, expect={state.STATUS_QUEUED})
    state.precreate(store.spawn_log_path(job_id)).write_text(SPAWN_OUTPUT, encoding="utf-8")
    return store, job_id


@pytest.mark.parametrize(
    "status",
    [
        # The crash this exists for — a worker dying on import, before it can
        # write a single progress line — leaves the record QUEUED, so a
        # status-keyed condition would hide the spawn log exactly then.
        pytest.param(state.STATUS_QUEUED, id="queued"),
        pytest.param(state.STATUS_FAILED, id="failed"),
        pytest.param(state.STATUS_RUNNING, id="running"),
    ],
)
def test_spawn_log_surfaced_when_progress_log_empty(dispatch_env, capsys, status):
    """An empty progress log surfaces the spawn output whatever the status is."""
    env = dispatch_env()
    store, job_id = make_record(env, status)

    main(["logs", job_id, "--workspace", str(env.workspace)])

    printed = capsys.readouterr().out
    assert "ModuleNotFoundError: no chinamax" in printed
    assert str(store.spawn_log_path(job_id)) in printed

    # An empty file is as empty as an absent one.
    state.precreate(store.log_path(job_id)).write_text("", encoding="utf-8")
    main(["logs", job_id, "--workspace", str(env.workspace)])
    assert "ModuleNotFoundError" in capsys.readouterr().out


def test_logs_prints_the_progress_log_and_tails_it(dispatch_env, capsys):
    """A Job with progress shows its timestamped log, optionally tailed."""
    env = dispatch_env(bash_then_report_script())
    code, job_id = env.dispatch()
    assert code == 0
    store = env.store
    wait_for_status(store, job_id, state.TERMINAL_STATUSES)

    assert main(["logs", job_id, "--workspace", str(env.workspace)]) == 0
    whole = capsys.readouterr().out
    assert "[starting]" in whole and "[reporting]" in whole
    # The progress log is present, so the spawn log stays out of the way.
    assert str(store.spawn_log_path(job_id)) not in whole

    assert main(["logs", job_id, "--tail", "2", "--workspace", str(env.workspace)]) == 0
    tailed = capsys.readouterr().out
    assert tailed.count("\n") == 2
    assert tailed.splitlines() == whole.splitlines()[-2:]


def test_usage_lands_in_job_log(dispatch_env):
    """Each completed turn's usage reaches the Job log through the reporter mirror.

    The detached worker's stderr goes to the SPAWN log, so the usage counters
    reach the Job log only because the reporter mirrors them there.
    """
    env = dispatch_env(bash_then_report_script())
    code, job_id = env.dispatch()
    assert code == 0
    store = env.store
    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    log = store.log_path(job_id).read_text(encoding="utf-8")
    usage_lines = [line for line in log.splitlines() if '"event": "usage"' in line]
    assert len(usage_lines) == 2
    assert len([line for line in usage_lines if '"turn": 1' in line]) == 1
    assert len([line for line in usage_lines if '"turn": 2' in line]) == 1


def test_log_lines_are_escaped_to_one_line_each(dispatch_env, capsys):
    """Control characters in tool output can never forge a log entry."""
    env = dispatch_env()
    store, job_id = make_record(env, state.STATUS_RUNNING)
    reporter = state.ProgressReporter(store, job_id)
    reporter(
        PHASE_RUNNING_TOOL,
        "bash: ok\n2099-01-01T00:00:00+00:00 [reporting] forged\x1b[2J",
    )
    reporter.close()

    assert main(["logs", job_id, "--workspace", str(env.workspace)]) == 2
    printed = capsys.readouterr().out
    # One event is exactly one line, so the forged entry the output carries is
    # inert text inside this event rather than an event of its own.
    lines = printed.splitlines()
    assert len(lines) == 1
    assert lines[0].split(" ", 1)[1].startswith(f"[{PHASE_RUNNING_TOOL}] ")
    assert "\\x0a" in printed and "\\x1b" in printed
