# Worker-Model Subagent Plugin — conventions

Inventory lives in `./repo-map.md`. Domain vocabulary lives in `./CONTEXT.md` — use its terms (Bridge Agent, Runtime, Job, Thread, Profile) in code, docs, and commits.

## Important things to note

- When launching your bridge agent, always use haiku or whatever is the cheapest model
- Prompt/contract text aimed at the haiku Bridge (spawn prompt, hook-injected contract) must be very explicit and stepwise — numbered, one action per rule — BUT token-lean: verbosity confuses it.
- When a decision conflicts with an existing ADR, amend that ADR in place (dated `**Amended <date>**` paragraph; quote the original when reversing; `git mv` the slug if it now contradicts the content). Never create a new ADR file for a reversal.

## How to run and test

The Runtime lives in its own conda env — never `py_automation`, never the repo's ambient python:

```bash
conda create -y -n chinamax python=3.12
conda run -n chinamax pip install -e '/home/klg2138/chinamax_plugin[test]'
conda run -n chinamax python -m pytest /home/klg2138/chinamax_plugin/tests -q
```

The editable install is what puts `chinamax` on the path; the suite imports the installed package, not a relative path. `pip install -e` leaves `src/chinamax.egg-info/` and `__pycache__/` byproducts behind — build artifacts, never committed.

## Dual-Host migration (2026-08-06)

The Runtime is shared by Claude Code and Codex. Process boundaries resolve a
Host explicitly (`--host` or `CHINAMAX_HOST`); native `PLUGIN_*` evidence wins
over Claude-compatible aliases. Claude keeps `~/.claude` and
`CLAUDE_PLUGIN_DATA`; Codex uses `~/.codex`, `PLUGIN_DATA`, and its own
`chinamax-codex` fallback. Job records carry `host`; Codex session records also
carry an ownership token. Do not add cross-Host path fallbacks.

The maintained Bridge contract is `skills/chinamax-bridge/SKILL.md`. Claude's
agent/command files and Codex's root skills are thin adapters/loaders. Codex
mutating task/setup actions require `codex --yolo` (`bypassPermissions`), while
Runtime `--read-only` remains the authoritative tool-layer posture. Codex
Bridge names are deterministic underscore-safe names using Terra at low
reasoning with no fork history. Codex CLI 0.146.0 and plugin 0.4.0 were
installed and live-tested in `/tmp/chinamax-codex-live`; DeepSeek is the current
hard-gate evidence, while other endpoint smokes were intentionally skipped.
The CLI still clamps the registered SessionEnd hook to 3 s and has no reliable
native teammate-stop event; both limits remain documented in
`docs/verification-report.md`.

Design/implementation decisions:
- Runtime is a custom agent loop modeled on the OpenAI Codex plugin's orchestration, written in Python 3 in a dedicated fresh conda env (not `py_automation`).
- Runtime speaks the providers' Anthropic-compatible Messages API, reusing the proven `/anthropic` base URLs, model strings, and keys from the implement-handoff skill verbatim.
- Plugin and Bridge Agent are both named `chinamax` (agent type `chinamax:chinamax`); providers are config Profiles — pro tiers only (pro-only reversed 2026-08-03 — see the model-override round below): deepseek, mimo, glm, minimax, kimi (flash/ultraspeed rows of implement-handoff are excluded). No default Profile: every dispatch names one explicitly.
- Visible commands (2026-07-30): /chinamax:task, status, profiles, setup. The internal seam verbs (result, cancel, resume, steer, logs) are no longer command files — the Bridge drives them; `reap` is a new internal verb the session hooks call in-process.
- Jobs have no wall-clock or turn caps; liveness-based supervision only (API inactivity → retried as transient failure ~6x backoff; bash per-command timeout default 10 min feeds back as an observation). Jobs die on exhausted API retries, explicit cancel, or their owning session ending (session-scoped, ADR 0004 reversed 2026-07-30).
- Every dispatch detaches immediately into a durable Job; exactly one PERSISTENT named haiku Bridge (`chinamax-<profile>-<task-slug>`) long-polls it (`status --wait --timeout-ms 120000` default, per-dispatch `poll=<seconds>` override, Bash timeout kept above the seam bound) in silence (no progress messages, no Job-id ack; a successful steer is silent), fires exactly ONE `SendMessage(to='main')` relay when the Job ends — the worker's response untouched, or the failure report — and then STAYS AVAILABLE for its Thread, classifying each later operator message (steer / resume / cancel / out-of-scope refusal). Steering is only ever the Bridge forwarding an operator message it classified — there is no `/chinamax:steer` command.
- Write-capable by default (--read-only opt-out); confinement is tool-layer (realpath-confined file tools, cwd-pinned bash + denylist + timeouts).
- Duplication guard: bridge/skill contract language + non-blocking Stop-hook notice of running Jobs.
- Durable state is Host-scoped: Claude uses ${CLAUDE_PLUGIN_DATA}/state/<repo-slug>-<hash>/, then $XDG_STATE_HOME/chinamax; Codex uses ${PLUGIN_DATA}/state/<repo-slug>-<hash>/, then $XDG_STATE_HOME/chinamax-codex. Each has its own sibling sessions/ registry, interpreter record, keys, overlays, and Jobs.
- Loop tools (rich set): bash, read_file, write_file, str_replace_edit, list_dir, grep, glob, apply_patch, report_result (mandatory completion; a single required `response` field carrying the worker's complete final answer, stored verbatim, no metadata fields, no runtime audit).
- Hooks (2026-07-30; sweep added 2026-07-31): shared Host-aware SessionStart/SessionEnd/Stop/UserPromptSubmit registration preserves Claude's synchronous lifecycle and adds Codex's token-safe detached reaper, SessionStart token export, and managed native-agent sync; PreToolUse loads the canonical Bridge contract and applies the Codex yolo backstop. The three Claude-side sweeps share `state.reap_stale_supervision` via `hooks.sweep_stale_supervision`; Codex deliberately does not adopt stranded orphan processes.
- Env: conda env `chinamax` (python 3.12) with the official `anthropic` SDK + pytest; plugin scripts invoke the env's absolute python path.
- Install: repo doubles as its own single-plugin marketplace (`.claude-plugin/marketplace.json` + plugin.json; agents/, commands/, hooks/, skills/, scripts/, src/, tests/); the canonical marketplace source is GitHub (kguo93/chinamax_plugin), the rpi4 git remote is a backup mirror only, and the local checkout stays the dev source (the editable Runtime install).
- Tests: pytest in tests/ against a hermetic fake Anthropic-Messages provider server (background, persistence, resume, cancel, confinement, timeouts, API-failure injection, session lifecycle).
- Live verification: the historical Claude matrix remains in `docs/verification-report.md`; the current Codex gate is DeepSeek-only in `/tmp/chinamax-codex-live` and covers read-only dispatch, active steer, and same-Bridge resume. Other Codex endpoints are intentionally not run for this acceptance.
- Keys: all five Profiles resolve from the selected Host's model-keys.env (`~/.claude` for Claude, `~/.codex` for Codex); values never cross Hosts.
- See docs/adr/ (0001–0014) for the recorded design decisions and their rejected alternatives.
- 2026-07-24 relay redesign (implemented in relay-01; recorded in amended ADRs 0003/0007/0008/0010): exactly one named haiku Bridge teammate per dispatch — explicit `model: haiku` override in the Agent call and the full contract in the spawn prompt (named spawns ignore agent frontmatter), Bridge forbidden to spawn subagents; long-poll default 900 s, per-dispatch `poll=<seconds>` (the `status --wait` `--timeout-ms` ceiling was lifted to 900 s while its 240 s default stayed put); mid-run relay errors only, terminal result with envelope stripped and worker prose untouched; new `/chinamax:steer` command for in-turn steering.
- API keys resolve via `~/.claude/model-keys.env`.
- 2026-07-27 relay-fidelity round (amended ADRs 0003/0007): `report_result` collapsed to a single required `response` field — the worker's complete final answer; the metadata fields (outcome/summary/lists) are gone and `result` renders the response bare under its `<id>  <status>` header. The Bridge relay is exactly ONE `SendMessage(to='main')` fired at terminal, never before (Job-id ack dropped; failures ride the same single relay; ending the turn without SendMessage(to='main') is not a relay), and the main agent regurgitates the relayed response verbatim.
- 2026-07-27 setup-bootstrap round: `/chinamax:setup` fixes as well as diagnoses — always-fix, no flag. A missing conda env is created (`conda create -y -n chinamax python=3.12`), missing deps are installed (`pip install -e '<repo>[test]'` under the env python), and a missing `~/.claude/model-keys.env` is scaffolded as a comments-only 0600 template (one commented `<api_key_env>=` line per resolved Profile, plus the recipe for extending to any Anthropic-compatible provider: an overlay row in `~/.claude/chinamax-profiles.json` + the matching key line). An existing key file is never touched; a healthy machine's setup mutates nothing; an absent conda is one bounded failure with advice; fixers are injectable seams like the probes; the `--json` report gains a `fixes` array and the exit code reflects the post-fix diagnosis.
- 2026-07-30 persistent-Bridge redesign (amended ADRs 0003/0004/0006/0008/0010): the Bridge is one PERSISTENT named teammate per Thread (`chinamax-<profile>-<task-slug>`, human-readable slug, never random) — dispatch → 120 s long-poll → one verbatim relay → then classify every later operator message (steer while running, resume once terminal, cancel on abandon, out-of-scope refusal). Jobs are session-scoped (ADR 0004 REVERSED): records carry `sessionId`/`bridgeName`, SessionEnd (incl. `/clear`) reaps the ending session's active Jobs (`cancelled`) and SessionStart reaps dead-session orphans (`interrupted`, a STORED terminal status now — but prune still grades the STORED status so a DERIVED-interrupted crash keeps its resumable Thread); a dead session's Job ids are never resumed. `resume <id>` refuses only when its own `lineageRoot` is still active (records also carry `resumedFrom`). Visible commands drop to task/status/profiles/setup; a `reap` verb and the SessionEnd/UserPromptSubmit/PreToolUse hooks are added; `status`/digests are bridge-first via the shared `state.render_job_row`. `SESSION_ID_VARIABLE` now points at `CHINAMAX_SESSION_ID` (the field the SessionStart hook actually exports), making `sessionId` lifecycle-load-bearing. Relay fidelity (ADR 0007) is untouched.
- 2026-07-31 bridge-death reap (amended ADRs 0003/0004): a Job must not outlive its Bridge. The Bridge's long-poll doubles as a supervision heartbeat — `status --wait` on an active Job stamps `supervisedAt`/`supervisionTimeoutMs` (`max`-only so an operator poll never lowers the bound, `touch=False` so the 60 s crash grace is untouched); the UserPromptSubmit/Stop/SessionStart hooks sweep this live session's Bridge-owned, still-`is_active` Jobs whose heartbeat aged past `2×bound+10 s` (`state.reap_stale_supervision`) and mark them `interrupted` (`bridge terminated`), stranding the Thread (a fresh `/chinamax:task` continues). Scope excludes bridgeless direct dispatches (never supervised) and DERIVED-`interrupted` crashes (stay resumable). Detection keys on staleness, NOT a `SubagentStop` event (live-probed unusable — fires every healthy turn-end, not on a mid-turn kill, and carries no teammate name). Surfaced through existing surfaces only (`status`/`result` reason line, the roster's dead-Bridge row); `hooks.json`, the commands, the agent file and the Bridge contract text are all unchanged.
- 2026-08-01 context-cache round (amended ADR 0001): request-prefix byte-stability is load-bearing for the providers' automatic prefix caching (DeepSeek bills cache-hit input ~10x cheaper; `cache_control` is ignored and never sent); the system template fronts the stable instructional body and trails the {workspace}/{posture} slots; the loop emits one `usage` event per completed turn (provider cache counters) to stderr and, via the reporter, the Job log; a resume's new Job id never enters request bytes — the Thread prefix survives the boundary verbatim.
- 2026-08-01 reasoning round (amended ADRs 0001/0006): always-on max-effort reasoning as Profile data (`request_extras` merged verbatim into every request; overlay wholesale-replace, `{}` disables, reserved keys — the five request keys plus `stream`/`timeout`/`extra_headers`/`extra_query` — rejected at top level and inside `extra_body`); thinking blocks are Thread history, replayed verbatim; glm ships budget-less `enabled`; `profiles` rows show the resolved extras; `anthropic` floor raised to the verified 0.118 (ADR 0009 amended).
- 2026-08-03 model-override round (amended ADR 0006): per-dispatch `model=<string>` on /chinamax:task (`task --model`; spec/exec field `model`) — pro-only REVERSED, tier names are marketing labels; no client-side validity check (the endpoint is the authority; an invalid string fails the Job's first non-transient response, PERMANENT-classified, and the Bridge relays the provider error); an explicit model is pinned to the Thread (`request.model` on the record, `create_resume` carries it, resume replays it verbatim) while an unpinned dispatch keeps re-resolution; extras/Bridge-naming unchanged; surfaces: `render_job_row` shows `profile (model)`, `status <id>` detail line; glossary: Pro term replaced by Default model / Pinned model.
