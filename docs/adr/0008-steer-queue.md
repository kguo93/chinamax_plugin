# Mid-run messages land in a steer queue

**Amended 2026-08-06.** Host selection and session tokens are persisted with the
Job so a steer cannot cross Claude/Codex state roots or session ownership.

Messages sent to the Bridge while its Job runs are written to the Job's steer queue; the Runtime drains the queue at each loop-iteration boundary and injects each steer into the Thread as a user message. This gives live mid-flight steering (parity with messaging a normal subagent) without interrupting an in-flight API turn. Rejected: deliver-after-completion (too late for "stop doing X") and reject-while-running (least subagent-like).

**Amended 2026-07-24**: alongside @-messaging the Bridge, a direct `/chinamax:steer` command enqueues a steer from the main context in-turn — same queue, same drain. It exists because the Bridge's long-poll (default 900 s per ADR 0003) caps how fast an @-message is picked up; the command path has no such latency.

**Amended 2026-07-30**: the `/chinamax:steer` command is deleted along with the rest of the internal command surface (steer/resume/cancel/result/logs) — the queue is now fed only by the Bridge forwarding operator messages it classified as steers (the seam CLI remains callable directly for emergencies). The latency the command existed to cover is instead bounded by the Bridge's shorter default long-poll (120 s, ADR 0003): a mailbox message reaches the steer queue at the next poll boundary, worst case ~2 minutes instead of ~15.
