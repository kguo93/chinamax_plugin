"""`result`: the stored payload for a finished Job, a refusal for an active one."""

from __future__ import annotations

import json

from chinamax import state
from chinamax.__main__ import main
from conftest import (
    REPORT_PAYLOAD,
    aged,
    bash_then_report_script,
    build_record,
    dead_pid,
    wait_for_status,
)


def test_result_finished(dispatch_env, capsys):
    """A completed Job returns its exact stored payload; --json emits it as stored."""
    env = dispatch_env(bash_then_report_script())
    code, job_id = env.dispatch()
    assert code == 0
    store = env.store
    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    workspace = str(env.workspace)

    assert main(["result", job_id, "--workspace", workspace]) == 0
    rendered = capsys.readouterr().out
    assert job_id in rendered
    assert state.STATUS_COMPLETED in rendered
    # The stored response, rendered bare after the header line.
    assert REPORT_PAYLOAD["response"] in rendered

    # --json is the bytes of jobs/<id>.result.json, not a re-serialization.
    assert main(["result", job_id, "--json", "--workspace", workspace]) == 0
    emitted = capsys.readouterr().out
    assert emitted == store.result_path(job_id).read_text(encoding="utf-8")
    assert json.loads(emitted) == REPORT_PAYLOAD

    # With no id, the latest completed Job.
    assert main(["result", "--workspace", workspace]) == 0
    assert job_id in capsys.readouterr().out


def test_result_refuses_active(dispatch_env, capsys):
    """An active Job exits 2 pointing at status; an unknown id is still 1."""
    env = dispatch_env()
    store = env.store
    job_id = build_record(
        store, workspace=env.workspace, status=state.STATUS_RUNNING
    )
    workspace = str(env.workspace)

    assert main(["result", job_id, "--workspace", workspace]) == 2
    refusal = capsys.readouterr().err
    assert job_id in refusal
    assert "status" in refusal

    # An unknown id is a resolution error, never "still active".
    assert main(["result", "task-nope-000000", "--workspace", workspace]) == 1
    assert "no Job matching" in capsys.readouterr().err


def test_result_without_payload_still_exits_zero(dispatch_env, capsys):
    """Every terminal Job exits 0 — the difference lives in the OUTPUT.

    `failed` is reserved for Runtime errors that carry only an errorMessage, and
    an interrupted Job carries neither, so a verb-local "no payload means
    nonzero" rule would tell the Bridge two different things about one state.
    """
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    failed = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_FAILED,
        completed_at=state.utc_now(),
    )
    store.update(failed, {"errorMessage": "permanent/http_status: HTTP 401"})

    assert main(["result", failed, "--workspace", workspace]) == 0
    rendered = capsys.readouterr().out
    assert state.STATUS_FAILED in rendered
    assert "HTTP 401" in rendered

    assert main(["result", failed, "--json", "--workspace", workspace]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": state.STATUS_FAILED,
        "errorMessage": "permanent/http_status: HTTP 401",
    }

    # An interrupted Job is terminal to the Bridge too, and its output is what
    # names the state and points at resume.
    interrupted = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_RUNNING,
        pid=dead_pid(),
        updated_at=aged(state.STALE_GRACE_S * 2),
    )
    assert main(["result", interrupted, "--workspace", workspace]) == 0
    rendered = capsys.readouterr().out
    assert state.STATUS_INTERRUPTED in rendered
    assert f"resume {interrupted}" in rendered


# ── Stop Policy hook (ADR 0016) ────────────────────────────────────────────────

from conftest import hook_group, report_turn, write_claude_settings, write_policy_settings

#: A Stop hook that BLOCKS the first report_result (stop_hook_active false) and
#: ALLOWS a later one (stop_hook_active true), recording the flag it saw.
_STOP_HOOK = """#!/usr/bin/env bash
input=$(cat)
printf '%s\\n' "$(printf '%s' "$input" | grep -o '\"stop_hook_active\":[a-z]*' | head -1)" >> "{evidence}"
if printf '%s' "$input" | grep -q '\"stop_hook_active\":true'; then
  exit 0
fi
printf 'blocked: finish once more' 1>&2
exit 2
"""


def test_stop_hook_blocks_then_releases(job_env, keyless_home, tmp_path):
    """A Stop hook rejects the first report_result; the second (stop_hook_active) completes."""
    write_policy_settings(hooks=True)
    evidence = tmp_path / "stop.seen"
    script = tmp_path / "stop_hook.sh"
    script.write_text(_STOP_HOOK.format(evidence=evidence), encoding="utf-8")
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        stop=[hook_group(f"bash {script}")],
    )
    env = job_env([report_turn(), report_turn()])

    assert env.run() == 0

    # The Job completed on the SECOND report attempt.
    assert env.result() == REPORT_PAYLOAD
    assert len(env.requests) == 2
    # The first report_result was rejected with the hook's reason.
    observations = env.observations()
    assert observations[0]["is_error"] is True
    assert "blocked: finish once more" in observations[0]["content"]
    # The Stop hook saw stop_hook_active flip false → true across the two firings.
    seen = [line for line in evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert seen == ['"stop_hook_active":false', '"stop_hook_active":true']
