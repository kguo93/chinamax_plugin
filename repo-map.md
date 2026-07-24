# repo-map

Repository for the `chinamax` worker-model subagent plugin for Claude Code. The Runtime is implemented through slice runtime-03 — the walking skeleton, the full tool registry with tool-layer confinement, and liveness-based supervision (inactivity watchdog, retry ladder, no caps) — and the jobs scope through slice jobs-03: durable detached dispatch, the lifecycle verbs, and the steer queue, so `task`/`task-worker`/`status`/`logs`/`result`/`cancel`/`resume`/`steer` all run over a per-workspace Job store that nothing reaps, with read-side crash detection, pruning, and durable mid-run steering drained at each loop boundary (with a crashed-worker relaunch reclaim). The surface scope is under way: slice surface-01 makes the plugin installable and adds the dispatching Bridge Agent plus the `/chinamax:task` command; the remaining commands, hooks, setup, result-handling skill (surface/02) and docs + live verification (surface/03) are still design-only.

- `pyproject.toml` — packaging for the `chinamax` Runtime: src layout, `anthropic` and `httpx` runtime deps, `[test]` extra, and the `data/*.json` package-data rule that ships the shipped Profiles.
- `.gitignore` — the editable install's byproducts (`__pycache__/`, `*.pyc`, `*.egg-info/`), which are never committed.
- `.claude-plugin/` — the plugin + marketplace manifest pair that makes the repo a self-installing single-plugin marketplace (`chinamax@deepseek-plugin`); see `.claude-plugin/repo-map.md`.
- `agents/` — Claude Code agent definitions. The plugin loader auto-scans this dir and registers EVERY `.md` as an agent, so it holds only component files: `chinamax.md`, the Bash-only Bridge Agent (`chinamax:chinamax`). No `CLAUDE.md`/`repo-map.md` trio here (it would register as bogus agents) — its conventions live in the root `CLAUDE.md`.
- `commands/` — Claude Code slash commands, likewise auto-scanned (component files only): `task.md` (`/chinamax:task`). No trio here for the same reason; conventions in the root `CLAUDE.md`.
- `src/` — the Runtime package (`chinamax`); see `src/repo-map.md`.
- `tests/` — pytest suite driving the Runtime and the Job supervisor against the hermetic fake provider, with real detached workers, plus the plugin-manifest / Bridge-contract / Bridge-path / task-command surface tests; see `tests/repo-map.md`.
- `CONTEXT.md` — domain glossary (Bridge Agent, Runtime, Job, Thread, Profile, Steer, Pro).
- `CLAUDE.md` — conventions and design-phase decisions (see `./repo-map.md` for inventory).
- `AGENTS.md` — stub pointing agents at `CLAUDE.md`.
- `settings.json.example` — earlier sketch of pointing Claude Code's env at DeepSeek's Anthropic-compatible endpoint; superseded by the custom-runtime decision but kept as reference.
- `docs/agents/` — Local markdown issue-tracker config (`issue-tracker.md`, `triage-labels.md`, `domain.md`) seeded from setup-matt-pocock-skills templates.
- `docs/plan/` — frozen implementation plans (one per issue, 9 total): `worker-model-subagent-plugin-{runtime,jobs,surface}-NN-*.md`, mirrored to `~/.claude/plans/`.
- `.scratch/` — planning artifacts from /to-plan many: `worker-model-subagent-plugin-{runtime,jobs,surface}/` each holding `PRD.md` + `issues/NN-*.md`.
- `docs/adr/` — architectural decision records 0001–0011: `0001-anthropic-messages-wire-format.md`, `0002-liveness-based-supervision.md`, `0003-detached-jobs-with-poll-relay-bridge.md`, `0004-jobs-outlive-claude-sessions.md`, `0005-tool-layer-confinement.md`, `0006-single-bridge-agent-with-profiles.md`, `0007-self-reported-results.md`, `0008-steer-queue.md`, `0009-anthropic-sdk-in-dedicated-conda-env.md`, `0010-duplication-guard.md`, `0011-hermetic-fake-provider-tests.md`.
