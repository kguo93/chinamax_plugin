# hooks/ — conventions

Inventory lives in `./repo-map.md`.

- **`hooks.json` registers SessionStart and Stop ONLY — never SessionEnd** (ADR
  0004): a Job outlives the session that started it, so nothing about a session
  ending may touch a running worker. The no-SessionEnd rationale is recorded in the
  file's own `description` string (surface/03 owns the README).
- **The `clear` matcher is deliberate.** `/clear` wipes context — exactly when
  inherited-Job awareness must be re-injected — and no model-facing leg of the guard
  survives a clear otherwise.
- **Timeouts are 10s, not 5.** Interpreter resolution can fall through to `conda
  run`, which pays process-startup cost before the hook body begins.
- **The command strings run the `scripts/` shims, not the python modules directly**
  (`"${CLAUDE_PLUGIN_ROOT}/scripts/session_start_hook"`). The shim is what resolves
  the env interpreter; naming `python -m …` here would skip that.
- **Only `hooks.json` is consumed here; the loader does not `.md`-component-scan
  `hooks/`,** so this trio is safe. Keep the hook LOGIC in `src/chinamax/hooks/`, not
  in this directory.
