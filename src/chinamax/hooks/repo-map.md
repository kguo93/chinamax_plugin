# repo-map — src/chinamax/hooks/

The session-lifecycle and Bridge-enforcement hook entrypoints. Python,
unit-testable with crafted stdin JSON, run through the `scripts/` shims and
registered in `hooks/hooks.json`. Jobs are session-scoped (ADR 0004, reversed
2026-07-30).

- `__init__.py` — shared helpers: `read_event()` (tolerant stdin-JSON reader →
  `{}` on empty/invalid) and `resolve_workspace(event)` (the three-rung
  cwd → `CLAUDE_PROJECT_DIR` → process-cwd resolution, each walked to the git
  toplevel via `state.resolve_workspace_root`).
- `session_start.py` — the SessionStart entrypoint: `_register_and_reap()` writes
  the session-liveness registry FIRST (`state.write_session_registry`), reaps a
  same-PID predecessor (the `/clear` path), then runs `state.reap_orphans()`;
  then writes the bounded running/recent Job digest to STDOUT (active + interrupted
  + the 5 most recent finished within 24 h, capped ~2 KB, bridge-first via
  `state.render_job_row`) and appends the `CHINAMAX_SESSION_ID`/`CLAUDE_PLUGIN_DATA`
  exports to `$CLAUDE_ENV_FILE`. Degrades to a clean exit 0 with empty stdout.
- `session_end.py` — the SessionEnd entrypoint: `state.reap_session()` kills the
  ending session's active Jobs (marks them `cancelled`), THEN removes the registry
  entry (reap first, registry-removal last, so a hook killed mid-reap degrades to
  the SessionStart orphan path). No reason filtering; always exit 0.
- `stop.py` — the Stop entrypoint: emits a single JSON object carrying ONLY
  `systemMessage` (bridge-first names + a `/chinamax:status` pointer) for ACTIVE
  Jobs, and nothing otherwise. Never a `decision` key.
- `user_prompt.py` — the UserPromptSubmit entrypoint (main only): scans every
  workspace store for THIS session's bridge-named Jobs, keeps each Bridge's newest
  Job (one row per Bridge, terminal ones included as idle/messageable), and injects
  the roster + explicit-addressing `ROUTING_RULE` as `additionalContext`. No output
  when the session owns no bridge-named Jobs.
- `bridge_contract.py` — the PreToolUse(Bash) entrypoint: for
  `agent_type == "chinamax:chinamax"` only, emits the `CONTRACT` constant (the one
  source of the injected classification contract, imported by the test) as
  subagent-scoped `additionalContext`; silent and exit 0 otherwise. Reinforcement,
  never a gate (ADR 0010).
