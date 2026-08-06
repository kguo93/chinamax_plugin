# repo-map — src/chinamax/hooks/

The Host-aware session-lifecycle and Bridge-enforcement hook entrypoints. Python,
unit-testable with crafted stdin JSON, run through the `scripts/` shims and
registered in `hooks/hooks.json`. Jobs are session-scoped (ADR 0004, reversed
2026-07-30); Codex has a detached token-safe SessionEnd reaper and pre-tool
mutation guard.

- `__init__.py` — shared helpers: `read_event()` (tolerant stdin-JSON reader →
  `{}` on empty/invalid), `resolve_workspace(event)` (the three-rung
  cwd → `CLAUDE_PROJECT_DIR` → process-cwd resolution, each walked to the git
  toplevel via `state.resolve_workspace_root`), and `sweep_stale_supervision(event)`
  (the Bridge-death sweep the three session hooks share → `state.reap_stale_supervision`
  keyed on the event's `session_id`, degraded to a stderr diagnostic on any failure).
- `session_start.py` — the SessionStart entrypoint: `_register_and_reap()` writes
  the session-liveness registry FIRST (`state.write_session_registry`), reaps a
  same-PID predecessor (the `/clear` path), runs `state.reap_orphans()`, then runs
  `sweep_stale_supervision(event)` (this live session's dead-Bridge reap);
  then writes the bounded running/recent Job digest to STDOUT (active + interrupted
  + the 5 most recent finished within 24 h, capped ~2 KB, bridge-first via
`state.render_job_row`) and emits the Host Session id/token plus the selected
  plugin-data export channel. Degrades to a clean exit 0 with empty stdout.
- `session_end.py` — the Host-aware SessionEnd entrypoint: Claude calls
  `state.reap_session()` synchronously, then removes its registry entry; Codex
  launches one detached token-safe `reap --session` process with a `flock` lock
  and returns immediately. No reason filtering; always exit 0.
- `stop.py` — the Stop entrypoint: runs `sweep_stale_supervision(event)` (after
  `resolve_workspace`, before the active filter), then emits a single JSON object
  carrying ONLY `systemMessage` (bridge-first names + a `/chinamax:status` pointer)
  for ACTIVE Jobs, and nothing otherwise. Never a `decision` key.
- `user_prompt.py` — the UserPromptSubmit entrypoint (main only): runs
  `sweep_stale_supervision(event)` first, then scans every workspace store for THIS
  session's bridge-named Jobs, keeps each Bridge's newest Job (one row per Bridge,
  terminal ones included as idle/messageable), and injects the roster +
  explicit-addressing `ROUTING_RULE` as `additionalContext`. `_roster_row` renders a
  swept-dead Bridge (`SUPERVISION_REAP_REASON`) as `dead — bridge terminated;
  dispatch a fresh task`, never the idle follow-up advice. No output when
  the session owns no bridge-named Jobs.
- `codex_pretool.py` — the Codex-only PreToolUse mutation backstop: strict Claude
  no-op, denies ChinamaX Agent/setup mutation unless permission mode is
  `bypassPermissions`, and never claims to replace Runtime read-only policy.
- `bridge_contract.py` — the PreToolUse(Bash) entrypoint: for an `agent_type`
  carrying the `chinamax` substring only (a NAMED spawn puts the teammate name,
  `chinamax-<profile>-<slug>`, in `agent_type`, not the `chinamax:chinamax` subagent
  type), emits the `CONTRACT` constant (the one source of the injected
  classification contract, imported by the test) as subagent-scoped
  `additionalContext`; silent and exit 0 otherwise. Reinforcement, never a gate
  (ADR 0010).
