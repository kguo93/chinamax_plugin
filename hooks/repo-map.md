# repo-map — hooks/

The plugin's hook registration. Claude Code reads `hooks/hooks.json`; the hook
LOGIC is python in `src/chinamax/hooks/` (run through the `scripts/` shims).

- `hooks.json` — registers five events (ADR 0004 reversed 2026-07-30):
  - `SessionStart` (matcher `startup|resume|clear|compact|fork` →
    `scripts/session_start_hook`, timeout 10) — registry write, orphan reap, digest.
  - `SessionEnd` (→ `scripts/session_end_hook`, timeout 30) — reap the ending
    session's active Jobs, remove its registry entry.
  - `Stop` (→ `scripts/stop_hook`, timeout 10) — non-blocking running-Jobs notice.
  - `UserPromptSubmit` (→ `scripts/user_prompt_hook`, timeout 10) — inject the
    live-Bridge roster + explicit-addressing routing rule into main.
  - `PreToolUse` (matcher `Bash` → `scripts/bridge_contract_hook`, timeout 10) —
    re-inject the Bridge classification contract into the chinamax Bridge only.
  The top-level `description` records the same rationale (JSON has no comments).
