# Jobs are session-scoped — SessionEnd kills, SessionStart reaps orphans

**Reversed 2026-07-30** (the original decision is quoted below). A Job never
outlives the Claude session that started it. Job records carry their owning
session id; a SessionEnd hook — which fires on `/clear` as well as on a real
exit — kills the owning session's still-active Jobs (whole process tree) and
marks their records terminal (`cancelled` at a clean session end). The
SessionStart hook additionally reaps orphans: any active Job whose owning
session is no longer alive (the crash path, where SessionEnd never fired) is
killed and marked `interrupted`, and the SessionStart digest reports what was
terminated instead of advertising inherited Jobs. A dead session's Job ids are
never resumed or re-attached; continuing work is only ever a live Bridge Agent
resuming its own Thread inside the owning session.

Why the reversal: the persistent-Bridge redesign (ADR 0003, amended
2026-07-30) makes the Bridge teammate the only interaction path with a Job,
and a Bridge dies with its session. A Job whose Bridge is gone is unreachable
by design — and an unreachable, write-capable worker burning API credit with
nobody to relay its result is a liability, not an asset. Long autonomous runs
remain fully supported *within* a session: liveness-based supervision
(ADR 0002) is unchanged. Rejected: refuse-only expiry (the orphan keeps
running unsupervised) and kill-without-reaper (a crashed session's orphans
would contradict the rule this ADR states).

**Amended 2026-07-31** (bridge-death reap): a Job now also never outlives its
**Bridge**. Supervision is heartbeat-stamped by the Bridge's own long-poll
(`status --wait` stamps `supervisedAt`/`supervisionTimeoutMs`); the session hooks
(UserPromptSubmit, Stop, SessionStart) sweep this live session's stale-supervised
active Jobs in-process (`reap_stale_supervision`) and mark them `interrupted` with
reason `bridge terminated`. The staleness threshold is `2×bound + 10 s` (bound =
the stamped `--timeout-ms` in seconds, else the 900 s `WAIT_TIMEOUT_MS` fallback).
Sweep scope is Bridge-owned, still-`is_active` records ONLY: a DERIVED-`interrupted`
crash keeps its resumable Thread (the Bridge stops stamping the moment its poll
reads terminal, so staleness alone cannot tell "Bridge dead" from "Job crashed,
Bridge idle"), and a bridgeless direct dispatch is never supervised (nothing
stamps it, so reaping it off `createdAt` would kill a healthy long-running
worker). The reaped Thread is stranded — resume is Bridge-only — so continuing is
a fresh `/chinamax:task`. Detection latency is event-bounded, not timed: the
sweep runs only on the owning session's UserPromptSubmit/Stop/SessionStart events,
so an idle session detects a dead Bridge no sooner than its next event, and never
before the threshold has passed — a session resumed or restarted inside the
threshold reaps nothing at SessionStart.

> **Original decision (2026-07, now reversed):** The Codex plugin's SessionEnd
> hook kills still-running jobs and deletes their records; we deliberately ship
> no SessionEnd hook at all. Jobs exist for indefinite autonomous work (the
> acceptance test is a 70+ minute run), so nothing about a Claude session
> ending may touch a running worker. New sessions learn about inherited Jobs
> through the SessionStart digest instead.
