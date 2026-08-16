# No wall-clock or turn caps; liveness-based supervision only

**Amended 2026-08-06.** Host lifecycle hooks and Codex's detached reaper do not
introduce a Job wall-clock or turn cap; they only enforce ownership/liveness.

Jobs exist for indefinite autonomous work (the acceptance test is a 70+ minute run that must not be killed), so the Runtime imposes no wall-clock timeout and no loop-turn cap. Supervision is liveness-based: a per-API-call inactivity timeout counts as a transient failure and is retried (~6 attempts, exponential backoff); bash commands get a per-command timeout (default 10 min, per-dispatch overridable) whose expiry is returned to the model as an observation, never a Job failure. A Job terminates only on exhausted API retries, an explicit cancel, or the model's own `report_result` call. Rejected: conventional bounded-autonomy caps (e.g. 4 h wall clock / 400 turns), which would eventually kill legitimate long runs.

**Amended 2026-08-15 (0.5.0).** A Stop Policy hook (ADR 0016) may now BLOCK a
`report_result` — the one terminal path — which continues the loop instead of
ending the Job. The no-caps stance holds regardless: there is deliberately NO
block-count cap. A permanently-blocking Stop hook is a runaway Job by design, and
cancel/steer is the remedy, exactly as for any other unbounded run. The block
answers the `report_result` tool_use with an error-flavored tool_result carrying
the hook reason (never a bare user turn — a provider 400) so the loop can send a
further turn. Cost shape: each blocked cycle re-spawns the full Stop hook set
(subprocess spawns and up-to-60 s timeouts per iteration), not just tokens.
