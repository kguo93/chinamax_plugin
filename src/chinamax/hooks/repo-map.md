# repo-map — src/chinamax/hooks/

The session-lifecycle hook entrypoints (surface/02). Python, unit-testable with
crafted stdin JSON, run through the `scripts/` shims and registered in
`hooks/hooks.json`. There is deliberately no SessionEnd entrypoint (ADR 0004).

- `__init__.py` — shared helpers: `read_event()` (tolerant stdin-JSON reader →
  `{}` on empty/invalid) and `resolve_workspace(event)` (the three-rung
  cwd → `CLAUDE_PROJECT_DIR` → process-cwd resolution, each walked to the git
  toplevel via `state.resolve_workspace_root`).
- `session_start.py` — the SessionStart entrypoint: writes the bounded running/
  recent Job digest to STDOUT (every active + interrupted Job plus the 5 most
  recent finished within 24 h, capped ~2 KB with `(+N more)`) and appends the
  shell-quoted `CHINAMAX_SESSION_ID` / `CLAUDE_PLUGIN_DATA` exports to
  `$CLAUDE_ENV_FILE` when set. Degrades to a clean exit 0 with empty stdout.
- `stop.py` — the Stop entrypoint: emits a single JSON object carrying ONLY
  `systemMessage` (job ids + a `/chinamax:status` pointer) for ACTIVE Jobs, and
  nothing otherwise (interrupted work is not in flight). Never a `decision` key.
