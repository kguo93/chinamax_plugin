# Documentation and the live verification gauntlet

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-surface/PRD.md
- ADRs: docs/adr/0002-liveness-based-supervision.md, docs/adr/0004-jobs-outlive-claude-sessions.md, docs/adr/0008-steer-queue.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

The proof and the manual: a README covering installation (marketplace add/install, conda env creation), configuration (profiles, override file, model-keys.env, per-dispatch flags), the command suite, and troubleshooting; then the live verification in a throwaway repo at `~/chinamax-verification/` — on deepseek: (1) a simple dispatch returning a correct result, (2) a mid-run Steer demonstrably injected into the Thread, (3) a 70+ minute Job (checklist of small edits interleaved with a ~5-minute sleep script) that survives un-killed and un-hung with poll-relay progress throughout and a clean structured result; then one smoke dispatch each on mimo, glm, minimax, kimi proving provider genericity. Record outcomes (durations, job ids, any anomalies) in a verification report in the repo.

## Acceptance criteria

- [ ] README suffices to install and configure on a fresh machine without reading source
- [ ] deepseek simple dispatch and mid-run Steer verified with transcript evidence
- [ ] 70+ minute deepseek Job completes: no self-kill, no hang, progress relayed, state intact across a deliberate Claude-session restart mid-run
- [ ] Four smoke dispatches (mimo, glm, minimax, kimi) each return a structured result
- [ ] Verification report committed with job ids, durations, and observed anomalies

## Blocked by

- surface/02-commands-hooks-guard
- runtime/03-liveness-ladder
