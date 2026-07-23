# Durable detached dispatch: task/status/logs through the CLI seam

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-jobs/PRD.md
- ADRs: docs/adr/0003-detached-jobs-with-poll-relay-bridge.md, docs/adr/0004-jobs-outlive-claude-sessions.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

The supervisor's core: a `task` subcommand that creates a durable Job record in the per-workspace state dir (`${CLAUDE_PLUGIN_DATA}/state/<repo-slug>-<hash>/`, XDG state fallback when unset), spawns the Runtime worker fully detached (new session, no inherited stdio), and returns the job id immediately; the worker rehydrates its request from the record, runs the loop, and mirrors phase/progress into `jobs/<id>.log` and the record. `status` lists running/recent Jobs with phase, elapsed, and a progress preview; `status <job> --wait` blocks up to ~240 s polling ~2 s; `logs <job>` prints the timestamped log. Concurrent Jobs coexist; nothing anywhere reaps state on session end.

## Acceptance criteria

- [ ] Dispatch returns a job id in under a second while the detached worker completes the scripted fake-provider Job afterwards
- [ ] Killing the dispatching process immediately after dispatch does not disturb the worker (background-execution test); state and result survive (persistence test)
- [ ] status shows running→completed transitions with phase and preview; status --wait returns early on completion
- [ ] Two concurrent Jobs in one workspace complete without corrupting state.json or each other's records
- [ ] State schema is versioned; no code path deletes or kills Jobs at any session boundary

## Blocked by

- runtime/01-walking-skeleton
