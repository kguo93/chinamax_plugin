# repo-map

Design-phase repository for the `chinamax` worker-model subagent plugin for Claude Code. No source yet.

- `CONTEXT.md` — domain glossary (Bridge Agent, Runtime, Job, Thread, Profile, Steer, Pro).
- `CLAUDE.md` — conventions and design-phase decisions (see `./repo-map.md` for inventory).
- `AGENTS.md` — stub pointing agents at `CLAUDE.md`.
- `settings.json.example` — earlier sketch of pointing Claude Code's env at DeepSeek's Anthropic-compatible endpoint; superseded by the custom-runtime decision but kept as reference.
- `docs/agents/` — Local markdown issue-tracker config (`issue-tracker.md`, `triage-labels.md`, `domain.md`) seeded from setup-matt-pocock-skills templates.
- `docs/plan/` — frozen implementation plans (one per issue, 9 total): `worker-model-subagent-plugin-{runtime,jobs,surface}-NN-*.md`, mirrored to `~/.claude/plans/`.
- `.scratch/` — planning artifacts from /to-plan many: `worker-model-subagent-plugin-{runtime,jobs,surface}/` each holding `PRD.md` + `issues/NN-*.md`.
- `docs/adr/` — architectural decision records 0001–0011: `0001-anthropic-messages-wire-format.md`, `0002-liveness-based-supervision.md`, `0003-detached-jobs-with-poll-relay-bridge.md`, `0004-jobs-outlive-claude-sessions.md`, `0005-tool-layer-confinement.md`, `0006-single-bridge-agent-with-profiles.md`, `0007-self-reported-results.md`, `0008-steer-queue.md`, `0009-anthropic-sdk-in-dedicated-conda-env.md`, `0010-duplication-guard.md`, `0011-hermetic-fake-provider-tests.md`.
