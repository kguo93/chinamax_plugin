"""Detached dispatch: the id comes back at once and the worker outlives its parent."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from chinamax import state
from chinamax.__main__ import execute_spec, main
from chinamax.liveness import PERMANENT
from chinamax.spec import parse_spec
from chinamax.transcript import read_messages
from conftest import (
    PROFILE,
    REPORT_PAYLOAD,
    bash_script,
    bash_then_report_script,
    report_turn,
    tool_results,
    wait_for,
    wait_for_status,
)
from fake_provider import status_fault

#: Long enough that the worker is demonstrably still running when its parent dies.
SLOW_COMMAND = "sleep 3; echo slow-turn-done"


def make_queued(store, prompt: str = "Do the task.") -> str:
    """Publish one queued record the way the dispatcher does, minus the spawn."""
    job_id = store.ensure().reserve_id()
    store.create(
        state.new_record(
            job_id,
            prompt=prompt,
            profile=PROFILE,
            write=True,
            workspace_root=store.workspace_root,
            log_file=store.log_path(job_id),
        )
    )
    return job_id


def dispatch_via_shell(env, prompt: str = "Do the task.") -> tuple[str, subprocess.Popen]:
    """Dispatch through an intermediate shell, so there is a live parent to kill.

    Returns the job id and the shell, whose whole process group the caller
    SIGKILLs. The shell lingers past the dispatch so the kill lands on a live
    group rather than on nothing.
    """
    marker = env.workspace / "dispatched-id"
    shell = subprocess.Popen(
        [
            "bash",
            "-c",
            f'{sys.executable} -m chinamax task --profile {PROFILE} '
            f'--workspace "{env.workspace}" -- "{prompt}" > "{marker}"; sleep 120',
        ],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert wait_for(lambda: marker.exists() and marker.read_text().strip(), 60.0)
    return marker.read_text(encoding="utf-8").strip(), shell


def kill_group(shell: subprocess.Popen) -> None:
    """SIGKILL an intermediate shell's whole process group — never pytest's."""
    group = os.getpgid(shell.pid)
    assert group != os.getpgid(0), "refusing to signal pytest's own process group"
    os.killpg(group, signal.SIGKILL)
    shell.wait(timeout=30)


def test_task_returns_id_fast(dispatch_env):
    """Dispatch returns an id in well under a second while the worker runs on."""
    env = dispatch_env(bash_script(SLOW_COMMAND))

    started = time.monotonic()
    code, job_id = env.dispatch()
    elapsed = time.monotonic() - started

    assert code == 0
    assert elapsed < 1.0, f"dispatch blocked for {elapsed:.2f}s"
    assert job_id.startswith("task-")
    record = env.store.read(job_id)
    assert record["status"] in state.ACTIVE_STATUSES
    assert record["request"]["prompt"] == "Do the task."
    assert record["request"]["profile"] == PROFILE


def test_background_execution(dispatch_env):
    """Killing the dispatching shell's process group does not disturb the worker."""
    env = dispatch_env(bash_script(SLOW_COMMAND))
    job_id, shell = dispatch_via_shell(env)
    store = env.store

    kill_group(shell)

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    assert record["result"] == REPORT_PAYLOAD
    # The worker is in its OWN session, so it was never in the killed group.
    assert "slow-turn-done" in tool_results(read_messages(store.transcript_path(job_id)))[0][
        "content"
    ]


def test_persistence_across_parent_death(dispatch_env):
    """State survives the dispatcher and is readable from a fresh process."""
    env = dispatch_env(bash_then_report_script())
    job_id, shell = dispatch_via_shell(env)
    kill_group(shell)
    store = env.store
    assert (
        wait_for_status(store, job_id, state.TERMINAL_STATUSES)["status"]
        == state.STATUS_COMPLETED
    )

    for path in (
        store.record_path(job_id),
        store.log_path(job_id),
        store.transcript_path(job_id),
        store.result_path(job_id),
    ):
        assert path.exists() and path.stat().st_size > 0, path
    assert read_messages(store.transcript_path(job_id))

    fresh = subprocess.run(
        [sys.executable, "-m", "chinamax", "status", job_id, "--workspace", str(env.workspace)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert fresh.returncode == 0, fresh.stderr
    assert job_id in fresh.stdout and state.STATUS_COMPLETED in fresh.stdout


def test_spawn_failure_marks_failed(dispatch_env, monkeypatch, tmp_path, capsys):
    """A spawn that genuinely fails leaves the Job failed, never queued forever."""
    env = dispatch_env(bash_then_report_script())
    not_an_interpreter = tmp_path / "not-python"
    not_an_interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(not_an_interpreter, 0o644)
    monkeypatch.setenv(state.WORKER_PYTHON_VARIABLE, str(not_an_interpreter))

    code, printed = env.dispatch()

    assert code != 0
    assert printed == ""
    assert "dispatch failed" in capsys.readouterr().err
    records, _ = env.store.load_records()
    assert len(records) == 1
    record = records[0]
    assert record["status"] == state.STATUS_FAILED
    assert "dispatch failed" in record["errorMessage"]
    assert record["completedAt"] is not None


def test_fast_worker_does_not_lose_pid_race(dispatch_env, monkeypatch, capsys):
    """A worker that finishes first makes the pid-write CAS fail HARMLESSLY.

    The barrier is a real one: the worker process, the state store and the
    compare-and-swap are all genuine, and only the moment the dispatcher reaches
    its bookkeeping write is pinned — otherwise the ordering this test exists to
    cover would be a coin flip.
    """
    env = dispatch_env([report_turn()])
    real_start_time = state.read_pid_start_time
    store = env.store

    def wait_then_read(pid: int):
        wait_for(
            lambda: any(
                (store.try_read(job) or {}).get("status") in state.TERMINAL_STATUSES
                for job in store.job_ids()
            ),
            60.0,
        )
        return real_start_time(pid)

    monkeypatch.setattr(state, "read_pid_start_time", wait_then_read)
    code, job_id = env.dispatch()

    assert code == 0, "the pid-write CAS failing is not a dispatch failure"
    assert "claimed its record before the pid write" in capsys.readouterr().err
    record = store.read(job_id)
    assert record["status"] == state.STATUS_COMPLETED
    assert record["result"] == REPORT_PAYLOAD


def test_second_claimant_refuses(dispatch_env):
    """Exactly one worker claims a queued record; the Job executes once."""
    env = dispatch_env(bash_then_report_script())
    provider = env.providers[PROFILE]
    store = env.store
    job_id = make_queued(store)

    assert main(["task-worker", "--job-id", job_id, "--state-dir", str(store.path)]) == 0
    assert store.read(job_id)["status"] == state.STATUS_COMPLETED
    assert len(provider.requests) == 2

    # A second claimant against the same record exits without running: the Job
    # is executed once, so the provider sees no further conversation.
    assert main(["task-worker", "--job-id", job_id, "--state-dir", str(store.path)]) == 0
    assert store.read(job_id)["status"] == state.STATUS_COMPLETED
    assert len(provider.requests) == 2

    # The other refusal arm: a record another live claimant already holds.
    running = make_queued(store)
    store.update(running, {"status": state.STATUS_RUNNING, "pid": 1}, expect={state.STATUS_QUEUED})
    assert main(["task-worker", "--job-id", running, "--state-dir", str(store.path)]) == 0
    after = store.read(running)
    assert after["status"] == state.STATUS_RUNNING and after["pid"] == 1
    assert not store.transcript_path(running).exists()


def test_cancel_before_claim_is_not_outrun(dispatch_env, capsys):
    """A terminal record is never overwritten with `running` by its own worker."""
    env = dispatch_env(bash_then_report_script())
    provider = env.providers[PROFILE]
    store = env.store
    job_id = make_queued(store)
    store.update(
        job_id,
        {"status": state.STATUS_CANCELLED, "completedAt": state.utc_now()},
        expect={state.STATUS_QUEUED},
    )

    assert main(["task-worker", "--job-id", job_id, "--state-dir", str(store.path)]) == 0

    assert provider.requests == []
    assert "exiting without running" in capsys.readouterr().err
    record = store.read(job_id)
    assert record["status"] == state.STATUS_CANCELLED
    assert record["startedAt"] is None
    assert not store.transcript_path(job_id).exists()


def test_bash_timeout_reaches_the_spec(dispatch_env):
    """`--bash-timeout-s` is recorded AND mapped onto the Runtime's spec field."""
    env = dispatch_env(bash_script("sleep 30"))

    code, job_id = env.dispatch("--bash-timeout-s", "1")

    assert code == 0
    store = env.store
    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["request"]["bashTimeoutSec"] == 1.0
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    # Accepted, stored, and actually applied: the command was cut short.
    observation = tool_results(read_messages(store.transcript_path(job_id)))[0]
    assert "TIMED OUT" in observation["content"]


def test_failure_payload_renders_onto_the_record(dispatch_env):
    """runtime/03's structured failure is the `failed` record's errorMessage.

    runtime/03 proves the runtime half only and leaves the record half to be
    verified where it is consumed — here. The compact rendering lands on the
    record; the FULL JSON payload stays in the Job log through the reporter.
    """
    env = dispatch_env([status_fault(401)])

    code, job_id = env.dispatch()

    assert code == 0
    store = env.store
    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_FAILED
    # A `failed` Job always carries an errorMessage, on exactly one line.
    message = record["errorMessage"]
    assert message and "\n" not in message
    for fragment in (PERMANENT, "http_status", "after 1 attempt(s)", "HTTP 401"):
        assert fragment in message, message
    assert record["result"] is None
    assert record["completedAt"] is not None

    # The full payload — including the fields the compact line drops — is in
    # the log, which is what makes the compact rendering safe to shorten.
    log = store.log_path(job_id).read_text(encoding="utf-8")
    assert '"event": "failure"' in log
    assert '"provider_body"' in log and "scripted HTTP 401" in log


def test_reporter_failure_does_not_fail_the_job(job_env):
    """A reporter that raises is logged and swallowed, never a Runtime failure."""
    env = job_env(bash_then_report_script())

    def exploding(phase: str, message: str) -> None:
        raise RuntimeError("reporter is broken")

    assert execute_spec(parse_spec(env.spec()), reporter=exploding) == REPORT_PAYLOAD
    assert env.result() == REPORT_PAYLOAD
