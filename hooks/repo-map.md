# repo-map — hooks/

The plugin's hook registration. Claude Code reads `hooks/hooks.json`; the hook
LOGIC is python in `src/chinamax/hooks/` (run through the `scripts/` shims).

- `hooks.json` — registers `SessionStart` (matcher `startup|resume|clear|compact`
  → `scripts/session_start_hook`, timeout 10) and `Stop` (→ `scripts/stop_hook`,
  timeout 10). There is deliberately NO `SessionEnd` entry (ADR 0004), recorded in
  the file's own top-level `description` (JSON has no comments).
