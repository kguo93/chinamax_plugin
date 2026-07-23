"""Liveness supervision: inactivity detection, the retry ladder, and its exits.

Every test drives the real loop against the hermetic fake provider and asserts
on observable seams — the recorded requests, the injected sleeper's record, the
reporter stream, the exit code and the transcript.
"""

from __future__ import annotations

import json
import time

import pytest

from chinamax.liveness import CONTENT_BEARING_EVENTS, PERMANENT, TRANSIENT
from chinamax.transcript import read_messages
from conftest import (
    BASH_COMMAND,
    BASH_TOOL_USE_ID,
    REPORT_PAYLOAD,
    REPORT_TOOL_USE_ID,
    Sleeper,
    events_named,
    loop_config,
    report_turn,
    tool_use_block,
    turn,
)
from fake_provider import (
    HANG_BOUND_S,
    drop_fault,
    eof_fault,
    hang_fault,
    ping_fault,
    status_fault,
    stream_error_fault,
)

#: Short enough to keep the suite fast, long enough that ordinary scheduling
#: does not trip it. The plan takes this trade knowingly: a >1 s stall on a
#: loaded runner would show up as a spurious extra retry.
FAST_INACTIVITY_S = 1.0
#: Used where the ladder must be exhausted, so six hangs are paid for.
SHORT_INACTIVITY_S = 0.3

SERVED_BODY = json.dumps(
    {"type": "error", "error": {"type": "api_error", "message": "upstream is on fire"}}
)
AUTH_BODY = json.dumps(
    {"type": "error", "error": {"type": "authentication_error", "message": "bad token"}}
)

#: The classification table as executable spec. Everything unlisted is permanent.
CLASSIFICATION_CASES = {
    "http_408": (status_fault(408), TRANSIENT),
    "http_409": (status_fault(409), TRANSIENT),
    "http_503": (status_fault(503), TRANSIENT),
    "dropped_connection": (drop_fault(), TRANSIENT),
    "premature_eof": (eof_fault(), TRANSIENT),
    "http_401": (status_fault(401), PERMANENT),
    "http_403": (status_fault(403), PERMANENT),
    "http_404": (status_fault(404), PERMANENT),
    "http_422": (status_fault(422), PERMANENT),
    "stream_invalid_request": (stream_error_fault("invalid_request_error"), PERMANENT),
    "stream_overloaded": (stream_error_fault("overloaded_error"), TRANSIENT),
}

#: Retry-After is a floor on the post-jitter sleep, then clamped to the cap.
RETRY_AFTER_CASES = {
    "floor": ("30", [30.0]),
    "clamped_to_cap": ("600", [300.0]),
    "http_date_ignored": ("Wed, 21 Oct 2015 07:28:00 GMT", [5.0]),
}


def failure_payload(err: str) -> dict:
    """Return the single terminal failure event the reporter emitted."""
    failures = events_named(err, "failure")
    assert len(failures) == 1, f"expected one failure event, got {failures}"
    return failures[0]


def test_midstream_hang_retried(job_env, capsys):
    """A stream that stalls after message_start is retried, and the Job completes."""
    env = job_env([hang_fault(), report_turn()])
    sleeper = Sleeper()

    started = time.monotonic()
    exit_code = env.run(
        env.spec(inactivity_timeout_s=FAST_INACTIVITY_S), config=loop_config(sleeper)
    )
    elapsed = time.monotonic() - started

    # The discriminator, and the reason the retry count is not enough on its
    # own: the scripted hang gives up by itself after HANG_BOUND_S, so a
    # watchdog that closes the response WITHOUT shutting the socket down still
    # ends up retrying and completing — twenty seconds later, once the fake
    # stops stalling. Against a real provider that moment never comes.
    assert elapsed < HANG_BOUND_S / 2, (
        f"the turn took {elapsed:.1f}s: the watchdog did not break the stalled "
        "read, and the attempt ended only because the fake stopped hanging"
    )
    assert exit_code == 0
    assert env.result() == REPORT_PAYLOAD
    retries = events_named(capsys.readouterr().err, "retry")
    assert len(retries) == 1
    assert retries[0]["failure_kind"] == "inactivity"
    assert retries[0]["classification"] == TRANSIENT
    assert sleeper.sleeps == [5.0]


def test_ping_keepalive_does_not_mask_hang(job_env, capsys):
    """Keepalives and SSE comments do not reset the watchdog; the hang still fires."""
    # Asserted directly because it is not observable through the loop today: the
    # SDK drops `ping` before it can reach the reset set, so adding "ping" to the
    # allowlist leaves the whole suite green. This line is what turns the rule
    # into a regression guard if a future SDK ever forwards keepalives.
    assert "ping" not in CONTENT_BEARING_EVENTS

    env = job_env([ping_fault(), report_turn()])
    sleeper = Sleeper()

    exit_code = env.run(
        env.spec(inactivity_timeout_s=FAST_INACTIVITY_S), config=loop_config(sleeper)
    )

    assert exit_code == 0
    assert env.result() == REPORT_PAYLOAD
    retries = events_named(capsys.readouterr().err, "retry")
    assert len(retries) == 1
    assert retries[0]["failure_kind"] == "inactivity"


def test_429_then_success(job_env):
    """Three rate limits then success: the ladder sleeps 5, 10 and 20 seconds."""
    env = job_env([status_fault(429)] * 3 + [report_turn()])
    sleeper = Sleeper()

    assert env.run(config=loop_config(sleeper)) == 0

    assert env.result() == REPORT_PAYLOAD
    assert sleeper.sleeps == [5.0, 10.0, 20.0]
    assert len(env.requests) == 4


def test_5xx_then_success(job_env):
    """Three server errors then success: the same ladder, the same sleeps."""
    env = job_env([status_fault(503)] * 3 + [report_turn()])
    sleeper = Sleeper()

    assert env.run(config=loop_config(sleeper)) == 0

    assert env.result() == REPORT_PAYLOAD
    assert sleeper.sleeps == [5.0, 10.0, 20.0]
    assert len(env.requests) == 4


@pytest.mark.parametrize("case", sorted(CLASSIFICATION_CASES))
def test_classification_table(job_env, capsys, case):
    """Each row of the table retries or fails fast exactly as it is written."""
    fault, expected = CLASSIFICATION_CASES[case]
    env = job_env([fault, report_turn()])
    sleeper = Sleeper()

    exit_code = env.run(
        env.spec(inactivity_timeout_s=FAST_INACTIVITY_S), config=loop_config(sleeper)
    )
    err = capsys.readouterr().err

    if expected == TRANSIENT:
        assert exit_code == 0
        assert env.result() == REPORT_PAYLOAD
        assert len(env.requests) == 2
        assert len(events_named(err, "retry")) == 1
        assert sleeper.sleeps == [5.0]
    else:
        assert exit_code != 0
        assert len(env.requests) == 1
        assert events_named(err, "retry") == []
        assert sleeper.sleeps == []
        assert failure_payload(err)["classification"] == PERMANENT


@pytest.mark.parametrize("case", sorted(RETRY_AFTER_CASES))
def test_retry_after_honored(job_env, capsys, case):
    """Retry-After floors the post-jitter sleep, then the cap clamps it."""
    value, expected = RETRY_AFTER_CASES[case]
    env = job_env([status_fault(429, headers={"Retry-After": value}), report_turn()])
    sleeper = Sleeper()

    assert env.run(config=loop_config(sleeper)) == 0

    assert sleeper.sleeps == expected
    err = capsys.readouterr().err
    assert events_named(err, "retry")[0]["sleep_s"] == expected[0]
    if case == "http_date_ignored":
        # Ignored with a warning rather than guessed at.
        assert len(events_named(err, "warning")) == 1


def test_ladder_exhaustion_fails_job(job_env, capsys):
    """Six attempts, five sleeps, a nonzero exit, and the provider body preserved."""
    env = job_env([status_fault(503, body=SERVED_BODY) for _ in range(6)])
    sleeper = Sleeper()

    assert env.run(config=loop_config(sleeper)) != 0

    # Exactly six requests proves no hidden SDK retry layer underneath.
    assert len(env.requests) == 6
    # Exactly five sleeps proves none is taken after the final attempt.
    assert sleeper.sleeps == [5.0, 10.0, 20.0, 40.0, 80.0]
    payload = failure_payload(capsys.readouterr().err)
    assert payload["attempt_count"] == 6
    assert payload["classification"] == TRANSIENT
    assert payload["status_code"] == 503
    assert payload["provider_body"] == SERVED_BODY


def test_inactivity_exhaustion_payload_nulls(job_env, capsys):
    """An inactivity exhaustion has no HTTP response to quote, and says so."""
    env = job_env([hang_fault() for _ in range(6)])
    sleeper = Sleeper()

    exit_code = env.run(
        env.spec(inactivity_timeout_s=SHORT_INACTIVITY_S), config=loop_config(sleeper)
    )

    assert exit_code != 0
    payload = failure_payload(capsys.readouterr().err)
    assert payload["failure_kind"] == "inactivity"
    assert payload["attempt_count"] == 6
    assert payload["status_code"] is None
    assert payload["provider_body"] is None
    assert len(sleeper.sleeps) == 5


def test_auth_error_fails_fast(job_env, capsys):
    """A 401 ends the run after one request, with the provider's message intact."""
    env = job_env([status_fault(401, body=AUTH_BODY), report_turn()])
    sleeper = Sleeper()

    assert env.run(config=loop_config(sleeper)) != 0

    assert len(env.requests) == 1
    assert sleeper.sleeps == []
    payload = failure_payload(capsys.readouterr().err)
    assert payload["classification"] == PERMANENT
    assert payload["attempt_count"] == 1
    assert payload["status_code"] == 401
    assert payload["provider_body"] == AUTH_BODY
    assert "bad token" in payload["exception_text"]


#: The two decisive half-stream shapes, each retried and then completed.
ATOMICITY_CASES = {
    "partial_tool_use": (
        [
            hang_fault(
                [tool_use_block(BASH_TOOL_USE_ID, "bash", {"command": BASH_COMMAND})],
                partial_last=True,
            ),
            turn([tool_use_block(BASH_TOOL_USE_ID, "bash", {"command": BASH_COMMAND})]),
            report_turn(),
        ],
        ["user", "assistant", "user", "assistant"],
        3,
    ),
    "complete_report_then_hang": (
        [
            hang_fault(
                [tool_use_block(REPORT_TOOL_USE_ID, "report_result", REPORT_PAYLOAD)]
            ),
            report_turn(),
        ],
        ["user", "assistant"],
        2,
    ),
}


@pytest.mark.parametrize("case", sorted(ATOMICITY_CASES))
def test_transcript_atomic_across_retries(job_env, capsys, case):
    """An aborted attempt leaves nothing behind: no partial turn, no duplicate."""
    script, expected_roles, expected_requests = ATOMICITY_CASES[case]
    env = job_env(script)
    sleeper = Sleeper()

    exit_code = env.run(
        env.spec(inactivity_timeout_s=FAST_INACTIVITY_S), config=loop_config(sleeper)
    )

    assert exit_code == 0
    assert env.result() == REPORT_PAYLOAD
    assert len(env.requests) == expected_requests

    messages = read_messages(env.transcript_path)
    assert [message["role"] for message in messages] == expected_roles
    # The retry replayed the snapshot; it did not append the aborted attempt.
    assert env.requests[0]["body"]["messages"] == env.requests[1]["body"]["messages"]

    tool_uses = [
        block
        for message in messages
        for block in message["content"]
        if block.get("type") == "tool_use"
    ]
    reports = [block for block in tool_uses if block["name"] == "report_result"]
    assert len(reports) == 1
    assert reports[0]["input"] == REPORT_PAYLOAD
    if case == "partial_tool_use":
        commands = [block for block in tool_uses if block["name"] == "bash"]
        assert len(commands) == 1
        assert commands[0]["input"] == {"command": BASH_COMMAND}

    # The retry is recorded, but as its own kind: it never replays.
    records = [
        json.loads(line)
        for line in env.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    retries = [record for record in records if record["kind"] == "retry"]
    assert len(retries) == 1
    assert len(events_named(capsys.readouterr().err, "retry")) == 1
