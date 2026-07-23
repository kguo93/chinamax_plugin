# Worker-Model Subagent Plugin — conventions

Inventory lives in `./repo-map.md`. Domain vocabulary lives in `./CONTEXT.md` — use its terms (Bridge Agent, Runtime, Job, Thread, Profile) in code, docs, and commits.

## How to run and test

The Runtime lives in its own conda env — never `py_automation`, never the repo's ambient python:

```bash
conda create -y -n chinamax python=3.12
conda run -n chinamax pip install -e '/home/klg2138/deepseek_plugin[test]'
conda run -n chinamax python -m pytest /home/klg2138/deepseek_plugin/tests -q
```

The editable install is what puts `chinamax` on the path; the suite imports the installed package, not a relative path. `pip install -e` leaves `src/chinamax.egg-info/` and `__pycache__/` byproducts behind — build artifacts, never committed.

Design/implementation decisions:
- Runtime is a custom agent loop modeled on the OpenAI Codex plugin's orchestration, written in Python 3 in a dedicated fresh conda env (not `py_automation`).
- Runtime speaks the providers' Anthropic-compatible Messages API, reusing the proven `/anthropic` base URLs, model strings, and keys from the implement-handoff skill verbatim.
- Plugin and Bridge Agent are both named `chinamax` (agent type `chinamax:chinamax`); providers are config Profiles — pro tiers only: deepseek, mimo, glm, minimax, kimi (flash/ultraspeed rows of implement-handoff are excluded). No default Profile: every dispatch names one explicitly.
- Commands: /chinamax:task, status, result, cancel, resume, setup, logs, profiles.
- Jobs have no wall-clock or turn caps; liveness-based supervision only (API inactivity → retried as transient failure ~6x backoff; bash per-command timeout default 10 min feeds back as an observation). Jobs die only on exhausted API retries or explicit cancel.
- Every dispatch detaches immediately (durable Job, no SessionEnd reaping); the Bridge Agent poll-relays progress and forwards mid-run messages into a steer queue drained each loop iteration.
- Write-capable by default (--read-only opt-out); confinement is tool-layer (realpath-confined file tools, cwd-pinned bash + denylist + timeouts).
- Duplication guard: bridge/skill contract language + non-blocking Stop-hook notice of running Jobs.
- Durable state under ${CLAUDE_PLUGIN_DATA}/state/<repo-slug>-<hash>/, falling back to $XDG_STATE_HOME/chinamax when unset (Codex layout, minus its SessionEnd cleanup).
- Loop tools (rich set): bash, read_file, write_file, str_replace_edit, list_dir, grep, glob, apply_patch, report_result (mandatory structured completion; final result is the worker's self-report verbatim, no runtime audit).
- Hooks: SessionStart injects a per-workspace running/recent Job digest; Stop emits a non-blocking running-Jobs notice; no SessionEnd hook.
- Env: conda env `chinamax` (python 3.12) with the official `anthropic` SDK + pytest; plugin scripts invoke the env's absolute python path.
- Install: repo doubles as its own single-plugin marketplace (`.claude-plugin/marketplace.json` + plugin.json; agents/, commands/, hooks/, skills/, scripts/, src/, tests/).
- Tests: pytest in tests/ against a hermetic fake Anthropic-Messages provider server (background, persistence, resume, cancel, confinement, timeouts, API-failure injection, session lifecycle).
- Live verification: full 3-part run on deepseek (simple dispatch; mid-run steer; 70+ min survival job) + one-shot smoke dispatch on mimo, glm, minimax, kimi — all in a throwaway repo at ~/chinamax-verification/.
- Keys: all five profiles resolve from ~/.claude/model-keys.env (GLM_API_KEY and MINIMAX_API_KEY appended from the implement-handoff literals).
- See docs/adr/ (0001–0011) for the recorded design decisions and their rejected alternatives.
- API keys resolve via `~/.claude/model-keys.env`.

Runtime conventions now in force (slice runtime-01, the walking skeleton):
- Every `~/.claude/...` path resolves through `Path.home()`, never a hardcoded `/home/...` — that is what lets the suite run keylessly under a temporary `HOME`.
- `model-keys.env` values are unquoted by shell rules (`shlex`), never a bare `split("=", 1)`: the real file single-quotes some values because it is normally bash-sourced.
- The client is built with `auth_token=` (bearer), never `api_key=`, and with `max_retries=0` so the SDK's own retries cannot nest under the Runtime's ladder. The shared exec entry pops `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` from `os.environ` first, so the Profile is the only source of endpoint and credential.
- The loop's termination keys on the ABSENCE of `tool_use` blocks, never on `stop_reason == "end_turn"` — `max_tokens` and `stop_sequence` produce the same tool-less turn.
- The transcript is write-ahead: the outgoing delta is appended and flushed BEFORE the API call. Later slices depend on this ordering; do not batch the writes.

Runtime conventions added by slice runtime-02 (the tool registry and its confinement) — the working detail lives in `src/chinamax/CLAUDE.md` and `src/chinamax/tools/CLAUDE.md`:
- Confinement is component-wise realpath containment, never a string prefix; the workspace realpath is resolved once per Job and carried on `ToolContext`.
- The bash denylist and the read-only write-shaped filter share ONE `shlex` lexer and match at command position, so a denied word inside a quoted argument still runs.
- Read-only enforcement is a single posture-filtered registry serving both the advertised schema and the dispatch table — schema omission alone is not the enforcement point.
- A bash timeout kills the child's whole process group and comes back as an observation; the Job continues (ADR 0002). `bash_timeout_s` is bounds-checked at spec validation.
- ADR 0005's residual risks (read-only bash bypasses, network egress, substitution evasions) are documented, not defended. Do not expand the denylist to chase them.
