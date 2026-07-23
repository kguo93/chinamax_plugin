# Liveness-based supervision: inactivity detection and retry ladder, no caps

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-runtime/PRD.md
- ADRs: docs/adr/0002-liveness-based-supervision.md, docs/adr/0011-hermetic-fake-provider-tests.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

The supervision layer of the loop: per-API-call streaming inactivity timeout treated as a transient failure; a retry ladder (~6 attempts, exponential backoff) covering inactivity, 429, 5xx, and connection errors; Job failure only on ladder exhaustion, recorded with the terminal error. Assert the absence of caps: no wall-clock limit and no turn limit exists anywhere in the loop — a scripted 100+-turn run completes.

## Acceptance criteria

- [ ] Fake-provider fault injection: mid-stream hang triggers inactivity detection and a successful retry resumes the Job
- [ ] 429 and 5xx responses retry with backoff; the Job succeeds when the fault clears within the ladder
- [ ] Ladder exhaustion fails the Job with the terminal error preserved — the runtime half (nonzero exit, structured failure payload through the reporter) proven here; the Job-record `status: failed` half is jobs-scope and lands when jobs/01 consumes the payload
- [ ] A scripted long run (100+ turns, simulated long wall-clock) completes with no cap firing; no cap constants exist in the loop configuration
- [ ] Transcript and result remain intact across retries (no duplicated or lost turns)

## Blocked by

- runtime/01-walking-skeleton
