# repo-map

Repository for the `chinamax` worker-model plugin for Claude Code and Codex. The shared Runtime owns Profiles, detached Jobs, confinement, liveness, state, and result fidelity; thin Host adapters own native routing, paths, lifecycle, manifests, and setup. Claude retains its persistent Haiku Bridge, while Codex uses root skills, deterministic underscore-safe Bridges with the fixed Codex **Bridge model** `gpt-5.6-terra`/low/no-fork settings, yolo mutation guards, and token-safe detached lifecycle cleanup. The Runtime's **Profile model** (`--model`) is the worker selection, distinct from the **Bridge model**.

- `pyproject.toml` — packaging for the `chinamax` Runtime: src layout, unconditional SDK deps, conditional macOS/Windows `psutil`/`filelock` deps, `[test]` extra, and the `data/*.json` package-data rule.
- `.gitignore` — the editable install's byproducts (`__pycache__/`, `*.pyc`, `*.egg-info/`), which are never committed.
- `LICENSE` — the GPL-2.0 license text (verbatim `gpl-2.0.txt`) the repo is published under.
- `.claude-plugin/` — the Claude plugin + marketplace manifest pair; see `.claude-plugin/repo-map.md`.
- `.codex-plugin/` — the Codex plugin manifest and full interface metadata.
- `.agents/plugins/marketplace.json` — the canonical Codex repo marketplace catalog; the legacy Claude marketplace remains under `.claude-plugin/`.
- `agents/` — Claude Code agent definitions. The plugin loader auto-scans this dir and registers EVERY `.md` as an agent, so it holds only component files: `chinamax.md`, the Bash-only Bridge Agent (`chinamax:chinamax`). No `CLAUDE.md`/`repo-map.md` trio here (it would register as bogus agents) — its conventions live in the root `CLAUDE.md`.
- `commands/` — Claude Code slash commands, likewise auto-scanned (component files only): `task.md` (`/chinamax:task`, the Bridge-dispatch command carrying the embedded persistent-Bridge contract) plus the thin `!`-launcher wrappers over `scripts/chinamax` — `status`, `profiles`, `setup`. The internal seam verbs (`result`/`logs`/`cancel`/`resume`/`steer`) are no longer exposed as commands (2026-07-30); the Bridge drives them on the operator's behalf. No trio here for the same reason; conventions in the root `CLAUDE.md`.
- `hooks/` — Host-specific hook registrations: `hooks.json` for Claude and
  `codex-hooks.json` for Codex (with Windows Git Bash `commandWindows` handlers);
  logic is `src/chinamax/hooks/`. See
  `hooks/repo-map.md`.
- `scripts/` — the shell shims that resolve the `chinamax` interpreter and exec the CLI / hook entrypoints (`chinamax`, `session_start_hook`, `session_end_hook`, `stop_hook`, `user_prompt_hook`, `bridge_contract_hook`, `_interpreter.sh`). See `scripts/repo-map.md`.
- `skills/` — shared Bridge contract plus Claude/Codex adapter skills (`chinamax-bridge`, `chinamax-task`, `chinamax-status`, `chinamax-profiles`, `chinamax-setup`, `chinamax-results`). See `skills/repo-map.md`.
- `src/` — the Runtime package (`chinamax`); see `src/repo-map.md`.
- `tests/` — pytest suite driving the Runtime and the Job supervisor against the hermetic fake provider, with real detached workers, plus the plugin-manifest / Bridge-contract / Bridge-path / task-command surface tests; see `tests/repo-map.md`.
- `README.md` — native Linux/macOS/Windows support matrix, Git Bash prerequisites,
  Host-native roots, setup, usage, troubleshooting, and honest mocked-validation
  limits.
- `CONTEXT.md` — domain glossary including Platform (orthogonal to Host), Bridge
  Agent, Runtime, Job, Thread, Profile, Steer, Default model, and Pinned model.
- `CLAUDE.md` — conventions and design-phase decisions (see `./repo-map.md` for inventory).
- `AGENTS.md` — stub pointing agents at `CLAUDE.md`.
- `docs/agents/` — Local markdown issue-tracker config (`issue-tracker.md`, `triage-labels.md`, `domain.md`) seeded from setup-matt-pocock-skills templates.
- `docs/plan/` — frozen implementation plans: `worker-model-subagent-plugin-relay-01-quiet-bridge.md` (the 2026-07-24 relay redesign; the nine original runtime/jobs/surface plans were removed once implemented), mirrored to `~/.claude/plans/`.
- `docs/verification-report.md` — the 0.4.3 verification scope and evidence record,
  distinguishing native Linux regression from mocked macOS/Windows coverage.
- `.scratch/` — planning artifacts from /to-plan many: `worker-model-subagent-plugin-{runtime,jobs,surface}/` each holding `PRD.md` + `issues/NN-*.md`.
- `docs/adr/` — architectural decision records 0001–0015, including the dual-Host
  runtime, Codex yolo/lifecycle decisions, and cross-platform portability.
