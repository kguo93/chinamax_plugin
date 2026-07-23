# Lifecycle verbs: result, cancel, resume, crash detection, pruning

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-jobs/PRD.md
- ADRs: docs/adr/0003-detached-jobs-with-poll-relay-bridge.md, docs/adr/0004-jobs-outlive-claude-sessions.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

The remaining lifecycle: `result <job>` returns the stored structured result and refuses while the Job is active; `cancel <job>` terminates the worker's process tree, marking the record cancelled with reason (refusing ambiguous cancels when multiple Jobs are active and no id given); `resume` continues the most recent finished Job's Thread with a follow-up prompt as a new Job sharing the transcript, refusing while any Job is active in the workspace; a stale-running detector reports crashed workers (recorded pid dead) honestly and leaves their Threads resumable; pruning keeps the most recent ~50 finished Jobs and never touches running ones. Id-prefix matching and latest-for-workspace defaults mirror Codex's job-control semantics.

## Acceptance criteria

- [ ] result returns the report_result payload for finished Jobs; refuses active Jobs with guidance to status
- [ ] cancel kills the whole detached process tree (verified dead), sets cancelled + reason; ambiguous cancel refuses with the active-job list
- [ ] resume creates a new Job continuing the prior transcript (fake provider sees the full prior history); refuses while a Job is active
- [ ] A worker killed with SIGKILL is detected as stale-running and reported as interrupted, its Thread resumable afterwards
- [ ] Pruning removes oldest finished records beyond ~50 (with their files) and never a running Job

## Blocked by

- jobs/01-durable-dispatch
