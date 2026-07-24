# Mid-run messages land in a steer queue

Messages sent to the Bridge while its Job runs are written to the Job's steer queue; the Runtime drains the queue at each loop-iteration boundary and injects each steer into the Thread as a user message. This gives live mid-flight steering (parity with messaging a normal subagent) without interrupting an in-flight API turn. Rejected: deliver-after-completion (too late for "stop doing X") and reject-while-running (least subagent-like).

**Amended 2026-07-24**: alongside @-messaging the Bridge, a direct `/chinamax:steer` command enqueues a steer from the main context in-turn — same queue, same drain. It exists because the Bridge's long-poll (default 900 s per ADR 0003) caps how fast an @-message is picked up; the command path has no such latency.
