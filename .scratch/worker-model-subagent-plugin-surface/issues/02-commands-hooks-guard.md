# Command suite, session hooks, and the duplication guard

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-surface/PRD.md
- ADRs: docs/adr/0010-duplication-guard.md, docs/adr/0004-jobs-outlive-claude-sessions.md, docs/adr/0007-self-reported-results.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

The rest of the surface: /chinamax:status, result, cancel, resume, logs, profiles (endpoint/model/key-presence, never values), and setup (conda env, deps, key entries per Profile, state-dir writability — offering env creation); the SessionStart hook injecting the running/recent Job digest; the Stop hook emitting the non-blocking running-Jobs notice; the result-handling skill rule forbidding Claude-side substitute implementations; deliberately no SessionEnd hook. Hook scripts are package entrypoints (resolved via the recorded env python) reading Job state through the same shared tolerant enumeration seam the CLI uses, and degrade gracefully on empty or corrupt state.

## Acceptance criteria

- [ ] Each command renders its CLI verb's output; result returns the worker's report verbatim
- [ ] SessionStart hook (crafted stdin, real script) emits the digest for running/recent Jobs and stays silent with none; corrupt state degrades without blocking the session
- [ ] Stop hook lists running Jobs non-blockingly and is silent otherwise; no SessionEnd hook is registered
- [ ] setup diagnoses missing env/deps/keys per Profile and state-dir problems in one pass
- [ ] Duplication-guard language present in Bridge contract and result-handling skill (installed and discoverable)

## Blocked by

- surface/01-installable-bridge
- jobs/02-lifecycle-verbs
- jobs/03-steer-queue
