> IMPLEMENTER: READ EVERY FILE BELOW IN FULL BEFORE WRITING ANY CODE.
> Do not infer or fill gaps — all authoritative context is here.
> - @/home/klg2138/deepseek_plugin/CONTEXT.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0002-liveness-based-supervision.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0011-hermetic-fake-provider-tests.md
> - @/home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-runtime/PRD.md
> - @/home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-runtime/issues/03-liveness-ladder.md
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-runtime-01-walking-skeleton.md
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-runtime-02-tool-registry-confinement.md
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-jobs-01-durable-dispatch.md

# Plan — Liveness-based supervision (no caps)

## Solution

Add the supervision layer to the loop: per-API-call streaming inactivity detection, a 6-attempt exponential-backoff retry ladder for transient failures (inactivity, 429, 5xx, connection errors) that is made the *only* retry layer by disabling the SDK's own, Job failure only on ladder exhaustion or a permanent classification, and the explicit absence of wall-clock/turn caps proven by test.

## Implementation Decisions

- `src/chinamax/liveness.py`: one attempt runner wrapping each streaming call. It takes an immutable snapshot of the message list and returns either a fully completed assistant message or a classified failure — nothing (tool_use blocks, `report_result` capture, transcript append) is exposed to `loop.py` before the attempt terminates at `message_stop`, so an aborted attempt can never half-apply a turn. A clean EOF *before* `message_stop` is an interrupted attempt, classified transient — never accepted as a completed turn.
- Inactivity watchdog (default 1800 s): the timer resets only on **content-bearing** events — `message_start`, `content_block_start/delta/stop`, `message_delta`, `message_stop`. It explicitly does **not** reset on `ping`, on SSE comments, or on raw bytes: the Messages wire format emits `ping` keepalives as ordinary parsed events, so resetting on "any parsed event" would let a stalled model be masked by a healthy proxy — the exact hang this slice exists to catch. The watchdog reads a **real monotonic clock**, not the injected clock (the injected clock governs backoff and turn timestamps only), so shrinking inactivity in tests works independently of clock advancement. It is armed per attempt and disarmed and joined in a `finally` before the next attempt begins, so a stale timer can never close a later attempt's stream; on expiry it closes the streaming response, and the resulting read error is caught *inside* the attempt runner and classified transient rather than escaping into `loop.py`.
- Classification is an explicit table, not a two-bucket rule. Transient: inactivity, premature EOF, 408, 409, 429, all 5xx, and connection/read-timeout errors. Permanent: 401, 403, 404, 422, and any other 4xx. A post-HTTP-200 `error` event is classified by its own error `type`/code, not by the enclosing 200 — an overloaded/rate-limit error retries, while `not_found_error`, `invalid_request_error`, and context-length errors fail fast instead of burning six attempts. Anything unlisted defaults to permanent. Permanent classification is the ladder's degenerate zero-retry case, which is how this plan reconciles fail-fast with the PRD's "failure only on exhausted retries" (PRD.md:42) rather than silently contradicting it.
- SDK retry authority: `provider.py` builds the client with `max_retries=0` and a structured `httpx.Timeout(read=inactivity + 60 s, connect=<small>)` — structured, not scalar, so a blackholed IP still fails connect promptly instead of inheriting a 1860 s connect budget. The watchdog always fires first; the SDK read timeout is only a backstop. Left at its defaults the `anthropic` SDK retries 408/409/429/5xx twice (`DEFAULT_MAX_RETRIES = 2`) under a 600 s read timeout (`DEFAULT_TIMEOUT`), which would silently turn 6 ladder attempts into up to 18 unlogged HTTP requests and pre-empt the 1800 s inactivity default. Slice 01 pins `anthropic>=0.37,<1` (its package decision) — a range within which these defaults may still drift — so the explicit `max_retries=0` is the defense, not the observed default. This is the single change slice 03 makes to slice 01's client construction (its client decision).
- Ladder: attempts 1..6, so exactly 5 sleeps and never a sleep after the final attempt. The sleep before attempt `k` (k = 2..6) is `min(5 * 2^(k-2), 300) s` → 5/10/20/40/80 s, with full jitter applied by an **injected jitter function** (production `uniform(0, computed)`; tests inject identity, so expectations never couple to a PRNG sequence); the 300 s cap binds only when a dispatch enlarges the ladder. `Retry-After` is honoured as a floor on the post-jitter sleep and then clamped to the same 300 s cap, so a hostile or absurd value cannot park a Job indefinitely; only delta-seconds form is parsed, and an HTTP-date or malformed value is ignored with a warning rather than guessed at. (This clamp bounds one retry sleep, not the Job — no wall-clock cap is introduced.) Exactly one `retry` event is emitted per retry decision, carrying attempt number, classification, and the slept duration, so "one retry" is a countable assertion.
- Failure seam: the runtime scope owns no state store (runtime/01's out-of-scope list), so both ladder exhaustion and permanent classification end the run by returning a terminal failure at the `exec` seam — nonzero exit plus a failure payload emitted as one structured JSON line through the progress reporter (stderr in this slice; jobs/01's detach decision spawns the worker with stderr redirected to `jobs/<id>.spawn.log`, so the reporter is the channel that survives detachment). Payload: `{failure_kind, classification, attempt_count, status_code, provider_body, exception_text}`, with `status_code`/`provider_body` nullable because inactivity and connection failures have no HTTP response; `provider_body` is the response text decoded UTF-8 lossy (bytes are not JSON-serializable) and preserved otherwise unmodified. jobs/01's terminal-write decision owns the record half of the mapping — its worker catch stores a compact `classification/attempt_count/status_code/exception_text` rendering of this payload as `errorMessage` on the `failed` record, with the full JSON line already in `jobs/<id>.log` via the reporter — so this slice proves the runtime half only; the record half is verified when jobs/01 consumes it.
- Atomicity: the canonical Thread history is appended only when an attempt completes; a failed or aborted attempt appends nothing to it. Retry events are a distinct record type in the JSONL that never participates in replay — the completed-turns-only narrowing of "written before/after every API call" that the PRD's Thread decision (PRD.md:43) now records, so a retry cannot leave a phantom turn. Retries replay from the in-memory snapshot, never by re-parsing the transcript, so a corrupt JSONL cannot fail a Job for the wrong reason.
- No caps: loop config deliberately contains no wall-clock or max-turn field; a code comment cites ADR 0002 so nobody "fixes" it. Cancellation, `report_result`, ladder exhaustion, and permanent-classification failure (the degenerate zero-retry case of the same ladder) are the only exits.
- Config: one `LoopConfig` dataclass in `src/chinamax/liveness.py` holds the inactivity timeout, ladder size, backoff base/cap, and the injected clock, sleeper, and jitter seams — the injection points the tests need to make backoff deterministic and instantaneous. Optional additive job-spec fields override the first four per dispatch, so slice-01/02-era specs stay valid and the fake-provider tests can shrink them. `spec.py` owns parsing and validation of those overrides and applies them over the defaults, mirroring how slice 02 bounds-checks `bash_timeout_s` (its job-spec-extension decision): inactivity must be finite and positive and ladder size an integer ≥ 1, and a violation fails spec validation fast rather than yielding an instantly-tripping watchdog or a ladder with no attempts.

## Acceptance Criteria (from the issue)

- [ ] Mid-stream hang triggers inactivity detection; a successful retry resumes the Job
- [ ] 429 and 5xx retry with backoff; Job succeeds when the fault clears within the ladder
- [ ] Ladder exhaustion fails the Job with terminal error preserved — the runtime half (nonzero exit, failure payload, reporter line) proven here; the Job-record/`status: failed` half is jobs-scope and lands when jobs/01 consumes this payload
- [ ] A scripted 100+-turn run under a simulated long wall-clock completes with no cap firing; no cap constants exist in loop configuration
- [ ] Transcript and result remain intact across retries — no duplicated or lost turns

## Tracking

- [ ] `liveness.py` attempt runner + classification table (incl. post-200 `error`-event typing and premature EOF)
- [ ] inactivity watchdog: content-bearing reset set excluding `ping`, real monotonic clock, per-attempt arm/disarm with contained close errors
- [ ] `provider.py`: `max_retries=0` + structured `httpx.Timeout(read=inactivity+60, connect=small)`
- [ ] retry ladder + injected-jitter backoff + `Retry-After` floor-then-clamp + one `retry` event per decision
- [ ] terminal failure seam (nonzero exec exit + nullable structured payload through the reporter)
- [ ] transcript atomicity across retries (completed turns only; retry events a separate record type)
- [ ] `LoopConfig` + optional additive job-spec overrides validated in `spec.py`
- [ ] no-caps config assertion + ADR comment
- [ ] Test suite below green

## Tests

All tests live in `tests/` and drive the real loop against slice 01's fake provider. Per the PRD's testing rule (PRD.md:48) they assert on observable seams — the fake provider's recorded requests, the injected sleeper's recorded sleeps, the reporter stream, the exit code, and the transcript — never on internal call sequences.

- `tests/test_liveness.py::test_midstream_hang_retried` — fake provider hangs after `message_start` on attempt 1, serves normally on attempt 2, inactivity shrunk to ~1 s: Job completes and the reporter shows exactly one `retry` event (issue AC bullet 1). The exact-count assertion accepts a known flake mode — a >1 s scheduling stall on a loaded runner would show a spurious second retry; 1 s is the deliberate trade against keeping the suite fast.
- `tests/test_liveness.py::test_ping_keepalive_does_not_mask_hang` — the provider emits `message_start` then a steady drip of `ping` events and SSE comments with no content events. The watchdog must still fire and retry, proving the reset set excludes keepalives; this is the distinguishing behavior of the watchdog design and would otherwise go unverified.
- `tests/test_liveness.py::test_429_then_success` / `test_5xx_then_success` — first N attempts return the fault, then success; with identity jitter injected the sleeper's recorded sequence equals 5/10/20 s exactly and the Job completes (AC bullet 2).
- `tests/test_liveness.py::test_classification_table` — parametrized over the table: 408, 409, 503 and a dropped connection retry; 401, 403, 404, 422 and a post-200 `invalid_request_error` event fail fast; a post-200 `overloaded_error` event retries. Turns the classification prose into executable spec, covering the rows the other tests miss.
- `tests/test_liveness.py::test_retry_after_honored` — `Retry-After: 30` on a 429 floors the sleep at 30 s; a second case with `Retry-After: 600` is clamped to the 300 s cap, and an HTTP-date value is ignored in favor of the computed backoff — pinning the floor/cap interaction rather than leaving it to the implementer.
- `tests/test_liveness.py::test_ladder_exhaustion_fails_job` — a persistent 503 on every attempt: the fake provider records exactly 6 requests (proving no hidden SDK retry layer), the sleeper records exactly 5 sleeps (proving none after the final attempt), exec exits nonzero, and the reporter's failure payload carries `provider_body` equal to the served body plus `attempt_count` 6 (AC bullet 3, runtime half).
- `tests/test_liveness.py::test_inactivity_exhaustion_payload_nulls` — ladder exhausted by repeated hangs rather than HTTP faults: `status_code` and `provider_body` are null and `failure_kind` names inactivity, exercising the payload's nullable path.
- `tests/test_liveness.py::test_auth_error_fails_fast` — 401 ends the run after exactly 1 recorded request with the provider message preserved and no sleeps recorded (the plan's own classification table, not a PRD story).
- `tests/test_no_caps.py::test_hundred_turn_run_completes` — 120 scripted micro-turns with the injected clock advanced past 4 h between turns complete with no cap firing; and `LoopConfig`'s field set equals an exact expected allowlist, so a cap added under any name (`max_duration`, `loop_limit`, …) fails the test rather than slipping past a name-pattern check (AC bullet 4). The allowlist guards the config surface only — a cap hidden as a module constant in `loop.py` would slip past it, which is why the 120-turn simulated-long-clock run, not the introspection, is the real no-caps proof.
- `tests/test_liveness.py::test_transcript_atomic_across_retries` — the decisive half-stream cases, run as two scripts: (a) a partial `tool_use` block then a hang, and (b) a complete `report_result` block then a hang before `message_stop`. Each retries and then completes; the final transcript holds exactly one copy of each turn with no partial turn from the aborted attempt, the recorded requests show no duplicated messages, and case (b) captures the result exactly once (AC bullet 5).

## Verification plan (run in main)

```bash
conda run -n chinamax pip install -e /home/klg2138/deepseek_plugin[test]
conda run -n chinamax python -m pytest /home/klg2138/deepseek_plugin/tests -q
```

## Out of scope

Cancellation delivery, including interrupting a blocked stream or a backoff sleep — the jobs scope owns the kill path and `cancel` terminates the whole worker process tree (jobs PRD story 7), so the liveness layer needs no interruptible boundary of its own; wall-clock/turn caps (rejected, ADR 0002); provider-specific quirk handling beyond the classification table above.
