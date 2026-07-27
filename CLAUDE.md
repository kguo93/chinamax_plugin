# Worker-Model Subagent Plugin — conventions

Inventory lives in `./repo-map.md`. Domain vocabulary lives in `./CONTEXT.md` — use its terms (Bridge Agent, Runtime, Job, Thread, Profile) in code, docs, and commits.

## Important things to note

- When launching your bridge agent, always use haiku or whatever is the cheapest model

## How to run and test

The Runtime lives in its own conda env — never `py_automation`, never the repo's ambient python:

```bash
conda create -y -n chinamax python=3.12
conda run -n chinamax pip install -e '/home/klg2138/chinamax_plugin[test]'
conda run -n chinamax python -m pytest /home/klg2138/chinamax_plugin/tests -q
```

The editable install is what puts `chinamax` on the path; the suite imports the installed package, not a relative path. `pip install -e` leaves `src/chinamax.egg-info/` and `__pycache__/` byproducts behind — build artifacts, never committed.

Design/implementation decisions:
- Runtime is a custom agent loop modeled on the OpenAI Codex plugin's orchestration, written in Python 3 in a dedicated fresh conda env (not `py_automation`).
- Runtime speaks the providers' Anthropic-compatible Messages API, reusing the proven `/anthropic` base URLs, model strings, and keys from the implement-handoff skill verbatim.
- Plugin and Bridge Agent are both named `chinamax` (agent type `chinamax:chinamax`); providers are config Profiles — pro tiers only: deepseek, mimo, glm, minimax, kimi (flash/ultraspeed rows of implement-handoff are excluded). No default Profile: every dispatch names one explicitly.
- Commands: /chinamax:task, status, result, cancel, resume, steer, setup, logs, profiles.
- Jobs have no wall-clock or turn caps; liveness-based supervision only (API inactivity → retried as transient failure ~6x backoff; bash per-command timeout default 10 min feeds back as an observation). Jobs die only on exhausted API retries or explicit cancel.
- Every dispatch detaches immediately (durable Job, no SessionEnd reaping); exactly one named haiku Bridge long-polls it (`status --wait --timeout-ms 900000` default, per-dispatch `poll=<seconds>` override, Bash timeout kept above the seam bound) in silence (no progress messages, no Job-id ack; a successful steer is silent), fires exactly ONE `SendMessage(to='main')` relay when the Job ends — the worker's response untouched, or the failure report — and forwards mid-run messages into a steer queue drained each loop iteration. A direct `/chinamax:steer` enqueues the same steer in-turn.
- Write-capable by default (--read-only opt-out); confinement is tool-layer (realpath-confined file tools, cwd-pinned bash + denylist + timeouts).
- Duplication guard: bridge/skill contract language + non-blocking Stop-hook notice of running Jobs.
- Durable state under ${CLAUDE_PLUGIN_DATA}/state/<repo-slug>-<hash>/, falling back to $XDG_STATE_HOME/chinamax when unset (Codex layout, minus its SessionEnd cleanup).
- Loop tools (rich set): bash, read_file, write_file, str_replace_edit, list_dir, grep, glob, apply_patch, report_result (mandatory completion; a single required `response` field carrying the worker's complete final answer, stored verbatim, no metadata fields, no runtime audit).
- Hooks: SessionStart injects a per-workspace running/recent Job digest; Stop emits a non-blocking running-Jobs notice; no SessionEnd hook.
- Env: conda env `chinamax` (python 3.12) with the official `anthropic` SDK + pytest; plugin scripts invoke the env's absolute python path.
- Install: repo doubles as its own single-plugin marketplace (`.claude-plugin/marketplace.json` + plugin.json; agents/, commands/, hooks/, skills/, scripts/, src/, tests/); the canonical marketplace source is GitHub (kguo93/chinamax_plugin), the rpi4 git remote is a backup mirror only, and the local checkout stays the dev source (the editable Runtime install).
- Tests: pytest in tests/ against a hermetic fake Anthropic-Messages provider server (background, persistence, resume, cancel, confinement, timeouts, API-failure injection, session lifecycle).
- Live verification: full 3-part run on deepseek (simple dispatch; mid-run steer; 70+ min survival job) + one-shot smoke dispatch on mimo, glm, minimax, kimi — all in a throwaway repo at ~/chinamax-verification/.
- Keys: all five profiles resolve from ~/.claude/model-keys.env (GLM_API_KEY and MINIMAX_API_KEY appended from the implement-handoff literals).
- See docs/adr/ (0001–0012) for the recorded design decisions and their rejected alternatives.
- 2026-07-24 relay redesign (implemented in relay-01; recorded in amended ADRs 0003/0007/0008/0010): exactly one named haiku Bridge teammate per dispatch — explicit `model: haiku` override in the Agent call and the full contract in the spawn prompt (named spawns ignore agent frontmatter), Bridge forbidden to spawn subagents; long-poll default 900 s, per-dispatch `poll=<seconds>` (the `status --wait` `--timeout-ms` ceiling was lifted to 900 s while its 240 s default stayed put); mid-run relay errors only, terminal result with envelope stripped and worker prose untouched; new `/chinamax:steer` command for in-turn steering.
- API keys resolve via `~/.claude/model-keys.env`.
- 2026-07-27 relay-fidelity round (amended ADRs 0003/0007): `report_result` collapsed to a single required `response` field — the worker's complete final answer; the metadata fields (outcome/summary/lists) are gone and `result` renders the response bare under its `<id>  <status>` header. The Bridge relay is exactly ONE `SendMessage(to='main')` fired at terminal, never before (Job-id ack dropped; failures ride the same single relay; ending the turn without SendMessage(to='main') is not a relay), and the main agent regurgitates the relayed response verbatim.
- 2026-07-27 setup-bootstrap round: `/chinamax:setup` fixes as well as diagnoses — always-fix, no flag. A missing conda env is created (`conda create -y -n chinamax python=3.12`), missing deps are installed (`pip install -e '<repo>[test]'` under the env python), and a missing `~/.claude/model-keys.env` is scaffolded as a comments-only 0600 template (one commented `<api_key_env>=` line per resolved Profile, plus the recipe for extending to any Anthropic-compatible provider: an overlay row in `~/.claude/chinamax-profiles.json` + the matching key line). An existing key file is never touched; a healthy machine's setup mutates nothing; an absent conda is one bounded failure with advice; fixers are injectable seams like the probes; the `--json` report gains a `fixes` array and the exit code reflects the post-fix diagnosis.
