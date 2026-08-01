"""Liveness-based supervision: inactivity detection and the retry ladder.

One attempt runner wraps each streaming call. It takes an immutable snapshot of
the message list and returns either a fully completed assistant message or a
classified failure — nothing reaches the loop before the attempt terminates at
``message_stop``, so an aborted attempt can never half-apply a turn. A clean EOF
*before* ``message_stop`` is an interrupted attempt, not a completed one.

Around it sits the ladder: transient failures are retried with exponential
backoff, permanent ones fail fast as the ladder's degenerate zero-retry case.
This module is the Runtime's ONLY retry layer — `provider.build_client` disables
the SDK's own so the two cannot nest.
"""

from __future__ import annotations

import json
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable

import anthropic
import httpx

from chinamax.spec import JobSpec

#: No content-bearing stream event for this long ends the attempt (ADR 0002).
DEFAULT_INACTIVITY_TIMEOUT_S = 1800.0
#: Attempts 1..N, so exactly N-1 sleeps and never one after the final attempt.
DEFAULT_LADDER_ATTEMPTS = 6
DEFAULT_BACKOFF_BASE_S = 5.0
#: Caps ONE retry sleep, never the Job: ADR 0002 forbids a wall-clock bound.
DEFAULT_BACKOFF_CAP_S = 300.0

TRANSIENT = "transient"
PERMANENT = "permanent"

#: The watchdog resets on these and ONLY these. `ping` keepalives and SSE
#: comments are deliberately absent: the Messages wire format emits `ping` as an
#: ordinary event, so resetting on "any traffic" would let a healthy proxy mask
#: a stalled model — the exact hang this module exists to catch. (The SDK drops
#: `ping` before it reaches us; the allowlist states the rule regardless.)
CONTENT_BEARING_EVENTS = frozenset(
    {
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    }
)

#: The classification table. Everything unlisted DEFAULTS to permanent.
TRANSIENT_STATUSES = frozenset({408, 409, 429})
PERMANENT_STATUSES = frozenset({401, 403, 404, 422})
#: A post-HTTP-200 `error` event is classified by its own error type, not by the
#: enclosing 200: an overloaded/rate-limit error is worth six attempts, a
#: not-found or invalid-request error is not.
TRANSIENT_ERROR_TYPES = frozenset({"overloaded_error", "rate_limit_error"})

_MIN_POLL_S = 0.01
_MAX_POLL_S = 0.25

__all__ = [
    "AttemptOutcome",
    "CONTENT_BEARING_EVENTS",
    "InactivityWatchdog",
    "LoopConfig",
    "PERMANENT",
    "RunFailure",
    "TRANSIENT",
    "backoff_sleep",
    "build_config",
    "classify",
    "emit_event",
    "parse_retry_after",
    "run_attempt",
    "stream_with_ladder",
]


def emit_event(name: str, details: dict) -> None:
    """Emit one structured progress event as a single JSON line.

    stderr is the channel in this slice; a detached worker is spawned with its
    stderr redirected into the Job's spawn log, so the reporter is the channel
    that reaches the Job log.

    Args:
        name: The event name (``retry``, ``failure``, ``warning``).
        details: The event's fields.
    """
    print(json.dumps({"event": name, **details}, sort_keys=True), file=sys.stderr, flush=True)


def full_jitter(computed: float) -> float:
    """Spread a backoff sleep over ``[0, computed]`` (production jitter)."""
    return random.uniform(0.0, computed)


@dataclass(frozen=True)
class LoopConfig:
    """Supervision knobs for one Job.

    This dataclass deliberately carries NO wall-clock deadline and NO maximum
    turn count, under any name. ADR 0002 rejects both outright: Jobs exist for
    indefinite autonomous work, and a cap would eventually murder a legitimate
    long run. Cancellation, ``report_result``, ladder exhaustion and a permanent
    classification are the only exits. Do not "fix" this by adding one.

    ``clock``, ``sleeper`` and ``jitter`` are seams: production uses the real
    clock, a real sleep and full jitter, while tests inject a stepping clock, a
    recording sleeper and identity jitter so expectations never couple to a PRNG
    sequence and the suite stays instantaneous.
    """

    inactivity_timeout_s: float = DEFAULT_INACTIVITY_TIMEOUT_S
    ladder_attempts: int = DEFAULT_LADDER_ATTEMPTS
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S
    clock: Callable[[], float] = time.time
    sleeper: Callable[[float], None] = time.sleep
    jitter: Callable[[float], float] = full_jitter


#: The job-spec fields that override the first four `LoopConfig` knobs.
OVERRIDABLE_FIELDS = (
    "inactivity_timeout_s",
    "ladder_attempts",
    "backoff_base_s",
    "backoff_cap_s",
)


def build_config(spec: JobSpec, base: LoopConfig | None = None) -> LoopConfig:
    """Apply a job spec's optional supervision overrides over the defaults.

    Args:
        spec: The validated job spec; its override fields are None when absent.
        base: The configuration to override, defaulting to `LoopConfig`'s
            defaults. In-process callers pass one to supply the clock, sleeper
            and jitter seams.

    Returns:
        The configuration this Job's loop runs with.
    """
    config = LoopConfig() if base is None else base
    overrides = {
        name: getattr(spec, name)
        for name in OVERRIDABLE_FIELDS
        if getattr(spec, name) is not None
    }
    return replace(config, **overrides) if overrides else config


class RunFailure(Exception):
    """A Job-ending provider failure: ladder exhaustion, or a permanent error.

    Carries the structured payload the exec seam reports and the jobs scope
    later renders onto the Job record.
    """

    def __init__(self, payload: dict) -> None:
        super().__init__(payload.get("exception_text") or payload["failure_kind"])
        self.payload = payload


@dataclass(frozen=True)
class AttemptOutcome:
    """One attempt's terminal state: a completed message, or a classified failure."""

    message: object | None = None
    classification: str | None = None
    failure_kind: str | None = None
    status_code: int | None = None
    provider_body: str | None = None
    exception_text: str | None = None
    retry_after_s: float | None = None

    @property
    def completed(self) -> bool:
        """Report whether the attempt reached ``message_stop``."""
        return self.message is not None


class InactivityWatchdog:
    """Closes a stalled stream after a silent interval.

    Armed for exactly one attempt and joined before the next one begins, so a
    stale timer can never close a later attempt's stream. It reads a REAL
    monotonic clock, never the injected one: the injected clock governs backoff
    and turn timestamps, and shrinking the inactivity bound in a test must work
    without advancing it.
    """

    def __init__(self, stream: object, timeout_s: float) -> None:
        """Prepare a watchdog over one open stream.

        Args:
            stream: The SDK message stream to close on expiry.
            timeout_s: The silent interval that ends the attempt.
        """
        self._stream = stream
        self._timeout_s = timeout_s
        self._last = time.monotonic()
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, name="chinamax-inactivity", daemon=True
        )
        self.fired = False

    def arm(self) -> None:
        """Start watching."""
        self._thread.start()

    def beat(self) -> None:
        """Record a content-bearing event."""
        self._last = time.monotonic()

    def disarm(self) -> None:
        """Stop watching and join, so no timer outlives its attempt."""
        self._done.set()
        self._thread.join()

    def _watch(self) -> None:
        poll = max(_MIN_POLL_S, min(_MAX_POLL_S, self._timeout_s / 10))
        while not self._done.wait(poll):
            if time.monotonic() - self._last >= self._timeout_s:
                # Set before closing: the loop thread reads this to tell an
                # inactivity abort from an ordinary dropped connection.
                self.fired = True
                self._close()
                return

    def _close(self) -> None:
        """Break the stalled read.

        ``shutdown()`` is the part that unblocks a read already parked in the
        loop thread — closing the socket alone leaves it parked on the file
        descriptor. Every failure here is swallowed: an exception raised in this
        thread would print a traceback onto the reporter's own stream.
        """
        try:
            response = getattr(self._stream, "response", None)
            if response is None:
                return
            network_stream = response.extensions.get("network_stream")
            raw = None if network_stream is None else network_stream.get_extra_info("socket")
            if raw is not None:
                try:
                    raw.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            response.close()
        except Exception:  # noqa: BLE001 - a watchdog failure must stay silent
            pass


def run_attempt(client: anthropic.Anthropic, request: dict, config: LoopConfig) -> AttemptOutcome:
    """Run one streaming call under the inactivity watchdog.

    Args:
        client: The SDK client built for the Profile.
        request: The Messages request keywords, including the message snapshot.
        config: The Job's supervision configuration.

    Returns:
        The completed assistant message, or a classified failure. Nothing is
        returned before ``message_stop``: a stream that ends cleanly short of it
        is an interrupted attempt, classified transient.
    """
    watchdog: InactivityWatchdog | None = None
    try:
        with client.messages.stream(**request) as stream:
            watchdog = InactivityWatchdog(stream, config.inactivity_timeout_s)
            watchdog.arm()
            try:
                for event in stream:
                    if event.type in CONTENT_BEARING_EVENTS:
                        watchdog.beat()
                    if event.type == "message_stop":
                        return AttemptOutcome(message=event.message)
            finally:
                # Disarmed and joined before any later attempt exists.
                watchdog.disarm()
    except Exception as error:  # noqa: BLE001 - every wire failure is classified
        # The watchdog's own close error is caught HERE and classified, never
        # allowed to escape into the loop as a crash.
        if watchdog is not None and watchdog.fired:
            return AttemptOutcome(
                classification=TRANSIENT,
                failure_kind="inactivity",
                exception_text=(
                    "no content-bearing stream event for "
                    f"{config.inactivity_timeout_s}s"
                ),
            )
        return classify(error)
    return AttemptOutcome(
        classification=TRANSIENT,
        failure_kind="premature_eof",
        exception_text="the provider closed the stream before message_stop",
    )


def classify(error: Exception) -> AttemptOutcome:
    """Classify a provider failure against the explicit table.

    Args:
        error: The exception the attempt raised.

    Returns:
        The classified failure, carrying whatever the provider gave us.
    """
    if isinstance(error, anthropic.APIStatusError):
        status = error.status_code
        if status == 200:
            # A post-200 `error` event: the enclosing 200 says nothing.
            classification = _classify_stream_error(error.body)
        elif status in TRANSIENT_STATUSES or 500 <= status < 600:
            classification = TRANSIENT
        elif status in PERMANENT_STATUSES or 400 <= status < 500:
            classification = PERMANENT
        else:
            classification = PERMANENT  # the DEFAULT for anything unlisted
        retry_after_s = (
            parse_retry_after(error.response.headers.get("retry-after"))
            if classification == TRANSIENT
            else None
        )
        return AttemptOutcome(
            classification=classification,
            failure_kind="http_status",
            status_code=status,
            provider_body=_response_body(error),
            exception_text=str(error),
            retry_after_s=retry_after_s,
        )
    if isinstance(error, (anthropic.APIConnectionError, httpx.HTTPError)):
        # Connection and read-timeout errors, and the read error the watchdog's
        # own close produces mid-stream.
        return AttemptOutcome(
            classification=TRANSIENT,
            failure_kind="connection",
            exception_text=f"{type(error).__name__}: {error}",
        )
    return AttemptOutcome(
        classification=PERMANENT,
        failure_kind="unexpected",
        exception_text=f"{type(error).__name__}: {error}",
    )


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header, delta-seconds form only.

    An HTTP-date or malformed value is ignored with a warning rather than
    guessed at — a wrong guess parks a Job for as long as the guess.

    Args:
        value: The raw header value, or None when absent.

    Returns:
        The delay in seconds, or None when there is nothing usable.
    """
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    emit_event("warning", {"message": f"ignoring unparsable Retry-After header {value!r}"})
    return None


def backoff_sleep(attempt: int, config: LoopConfig, retry_after_s: float | None) -> float:
    """Compute the sleep between ``attempt`` and the next one.

    ``Retry-After`` is a FLOOR on the post-jitter sleep and is then clamped to
    the same cap, so a hostile or absurd value cannot park a Job indefinitely.

    Args:
        attempt: The attempt that just failed, 1-based.
        config: The Job's supervision configuration.
        retry_after_s: The provider's ``Retry-After``, when it gave a usable one.

    Returns:
        The number of seconds to sleep.
    """
    computed = min(config.backoff_base_s * 2 ** (attempt - 1), config.backoff_cap_s)
    sleep_s = config.jitter(computed)
    if retry_after_s is not None:
        sleep_s = max(sleep_s, retry_after_s)
    return min(sleep_s, config.backoff_cap_s)


def stream_with_ladder(
    client: anthropic.Anthropic,
    *,
    model: str,
    max_tokens: int,
    system: str,
    tools: list[dict],
    messages: Iterable[dict],
    request_extras: dict | None = None,
    config: LoopConfig,
    on_retry: Callable[[dict], None] | None = None,
) -> object:
    """Drive one assistant turn through the retry ladder.

    Every attempt replays the same immutable snapshot of the history — never a
    re-parse of the transcript, so a torn JSONL line cannot fail a Job for the
    wrong reason.

    Args:
        client: The SDK client built for the Profile.
        model: The Profile's model string.
        max_tokens: The Profile's output bound.
        system: The Job's system prompt.
        tools: The advertised tool schemas.
        messages: The conversation so far.
        request_extras: Extra Messages-request keywords merged into every
            attempt's request verbatim — constant for the life of the Job.
        config: The Job's supervision configuration.
        on_retry: Called once per retry decision with that decision's fields.

    Returns:
        The completed assistant message.

    Raises:
        RunFailure: On a permanent classification or ladder exhaustion.
    """
    snapshot = tuple(messages)
    outcome = AttemptOutcome()
    for attempt in range(1, config.ladder_attempts + 1):
        request = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "tools": tools,
            "messages": list(snapshot),
            **(request_extras or {}),
        }
        outcome = run_attempt(client, request, config)
        if outcome.completed:
            return outcome.message
        if outcome.classification == PERMANENT:
            # The ladder's degenerate zero-retry case: six attempts at a 401
            # would only burn six minutes on the same answer.
            raise RunFailure(_failure_payload(outcome, attempt))
        if attempt == config.ladder_attempts:
            break
        sleep_s = backoff_sleep(attempt, config, outcome.retry_after_s)
        details = {
            "attempt": attempt,
            "classification": outcome.classification,
            "failure_kind": outcome.failure_kind,
            "sleep_s": sleep_s,
        }
        emit_event("retry", details)
        if on_retry is not None:
            on_retry(details)
        config.sleeper(sleep_s)
    raise RunFailure(_failure_payload(outcome, config.ladder_attempts))


def _failure_payload(outcome: AttemptOutcome, attempt_count: int) -> dict:
    """Render one terminal failure for the reporter.

    ``status_code`` and ``provider_body`` are nullable on purpose: inactivity
    and connection failures have no HTTP response to quote.
    """
    return {
        "failure_kind": outcome.failure_kind,
        "classification": outcome.classification,
        "attempt_count": attempt_count,
        "status_code": outcome.status_code,
        "provider_body": outcome.provider_body,
        "exception_text": outcome.exception_text,
    }


def _classify_stream_error(body: object) -> str:
    """Classify a post-HTTP-200 ``error`` event by its own error type."""
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("type") in TRANSIENT_ERROR_TYPES:
            return TRANSIENT
    return PERMANENT


def _response_body(error: anthropic.APIStatusError) -> str | None:
    """Return the provider's response body, decoded UTF-8 lossy and unmodified.

    Bytes are not JSON-serializable, and a body that was never read — a stream
    that failed after its 200 — yields None rather than an invented value.
    """
    try:
        return error.response.content.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - an unread or torn body is simply absent
        return None
