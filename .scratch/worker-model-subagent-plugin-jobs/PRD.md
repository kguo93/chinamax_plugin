# PRD — chinamax Jobs (durable detached lifecycle)

Scope: ADRs 0003, 0004, 0008. Part 2 of 3 (siblings: runtime, surface).

## Problem Statement

Long-running worker-model tasks die with the wrapper that started them: if the Claude session ends, the terminal closes, or the supervising subagent is reaped, today's delegation approaches lose the work, the logs, and the provider conversation. The operator needs 70-minute-plus autonomous Jobs that survive all of that, stay observable, accept mid-run guidance, and remain resumable afterwards.

## Solution

A task supervisor around the Runtime: every dispatch immediately creates a durable Job record and spawns a fully detached worker process that owns the Runtime loop. State, logs, Thread transcripts, and steer queues live in a per-workspace directory under the plugin data dir that nothing ever reaps. A CLI (the same one the Bridge Agent and slash commands call) provides task/status/result/cancel/resume/logs, with bounded `status --wait` polling so a caller can relay progress without ever tying the Job's fate to its own.

## User Stories

1. As the operator, I want every dispatch to detach immediately into a durable Job, so that no task's survival ever depends on the Claude wrapper staying alive.
2. As the operator, I want Jobs to keep running when the Claude session ends (no SessionEnd reaping — deliberate Codex deviation), so that a 70-minute run started at lunch is finished by dinner.
3. As the operator, I want Job state, logs, Thread transcript, and steer queue in `${CLAUDE_PLUGIN_DATA}/state/<repo-slug>-<hash>/` (falling back to `$XDG_STATE_HOME/chinamax` when that variable is unset, for bare-CLI callers), so that each workspace's history is isolated and out of the repo.
4. As Claude, I want `status` to show running/recent Jobs with phase, elapsed time, and a progress preview, so that I can report concisely without reading raw logs.
5. As Claude, I want `status <job> --wait` to block up to a bounded window (~4 min) and return on change, so that the Bridge can poll-relay cheaply in a loop.
6. As the operator, I want `result <job>` to return the stored structured result (and refuse while the Job is active), so that finished work is retrievable in any later session.
7. As the operator, I want `cancel <job>` to terminate the worker process tree and mark the Job cancelled with a reason, so that a runaway Job has exactly one brake.
8. As the operator, I want `resume` to continue the most recent finished (or interrupted) Job's Thread with a follow-up prompt (refusing while one is active), so that follow-up work keeps full provider context.
9. As the operator, I want a steer written while a Job runs to be queued durably and injected at the next loop iteration, so that "stop touching module X" lands within one turn.
10. As the operator, I want `logs <job>` to show the timestamped progress log, so that I can audit what a worker actually did.
11. As the operator, I want Job records to survive worker crashes with a detectable stale-running state (recorded pid no longer alive), so that such Jobs are reported honestly as interrupted (a derived read-side observation, never a stored status) and remain resumable from their Thread.
12. As Claude, I want dispatch to return the job id immediately, so that I am never blocked on a long run to acknowledge it started.
13. As a maintainer, I want the state schema versioned and additive, so that old Job records stay readable across plugin upgrades.
14. As the operator, I want concurrent Jobs in one workspace to coexist without clobbering each other's state, so that two dispatches never corrupt the store.
15. As a maintainer, I want every lifecycle behavior exercisable against the fake provider with real detached processes, so that durability claims are tested, not asserted.

## Implementation Decisions

- Dispatch path (ADR 0003): `task` subcommand creates the Job record (id `task-<base36-time>-<rand>`), then spawns the worker via double-detach (`start_new_session=True`, stdio detached to log files, no inherited pipes); the worker re-reads its persisted request and runs the Runtime loop.
- No SessionEnd hook exists at all (ADR 0004); nothing ever deletes or kills Jobs on session boundaries. State pruning keeps the most recent ~50 finished Jobs per workspace (Codex parity), never touching running ones.
- State layout (Codex-derived): per-workspace dir keyed `<repo-basename>-<sha256[:16] of realpath>` on the git-toplevel-resolved workspace root; `state.json` (derived id index, rebuilt from records), `jobs/<id>.json` (full record), `jobs/<id>.log` (timestamped progress log), `jobs/<id>.spawn.log` (worker stdio before the loop owns logging), `jobs/<id>.thread.jsonl` (Thread transcript), `jobs/<id>.result.json` (the Runtime's verbatim result artifact), `jobs/<id>.steer/` (queued steer messages as ordered files).
- Job record fields (Codex-derived): id, title/summary, profile, write flag, workspaceRoot, sessionId (originating), status (queued|running|completed|failed|cancelled), phase, pid, pidStartTime (pid-reuse guard), timestamps, logFile, result payload, request (for worker rehydration), errorMessage, schemaVersion.
- Steer queue (ADR 0008): `steer <job> <message>` appends an ordered file; the Runtime drains the queue at each loop-iteration boundary into user messages; drained steers are recorded in the log and transcript.
- Status/result/cancel/resume selection semantics mirror Codex's job-control: id-prefix matching; latest-for-workspace defaults; refuse ambiguous cancel with multiple active Jobs; resume refuses while an active Job runs. One shared exit-code convention across verbs — 0 terminal, 2 still active, 1 usage/resolution error; a derived-interrupted Job reads 0 with the distinction carried in the output.
- Progress: the worker updates phase/log through a progress reporter mirrored into `jobs/<id>.log` and the record; `status --wait` polls with a ~2 s interval up to a ~240 s bound per call.
- All subcommands are one CLI entrypoint (the seam) — the identical interface the Bridge Agent, slash commands, and hooks call.

## Testing Decisions

- Good tests treat the CLI as the seam and the state dir + workspace as observable outputs; they spawn the real detached worker against the fake provider — no mocked process layer.
- Covered behaviors: background execution (dispatcher exits, worker continues), persistence across simulated session end (parent killed, Job finishes), resume with preserved Thread context, cancellation (process tree dead, status cancelled), steer delivery ordering, crash detection (stale pid), concurrent Jobs, state pruning never touching running Jobs.
- Prior art: the Codex plugin's state.mjs/tracked-jobs.mjs/job-control.mjs semantics studied during design; sibling runtime PRD's fake provider is reused.
- Tests in `tests/`, pytest, hermetic (ADR 0011).

## Out of Scope

- The agent loop internals, tools, confinement, retry ladder (runtime PRD).
- Bridge Agent contract, slash commands, hooks, install, live verification (surface PRD).
- Cross-workspace job listing, multi-repo dashboards, broker/multiplexer daemons (Codex's broker is not adopted — one worker process per Job needs no multiplexing).

## Further Notes

The originating sessionId is recorded for provenance and digest rendering only — no lifecycle behavior may ever key off it (ADR 0004). Vocabulary per CONTEXT.md.
