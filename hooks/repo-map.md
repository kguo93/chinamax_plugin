# repo-map — hooks/

The plugin's shared Host-aware hook registration. Claude Code and Codex read the
same `hooks/hooks.json`; the hook LOGIC is Python in `src/chinamax/hooks/` (run
through the `scripts/` shims), with each handler resolving and filtering its Host.

- `hooks.json` — registers five events (ADR 0004 reversed 2026-07-30):
  - `SessionStart` (matcher `startup|resume|clear|compact|fork` →
    `scripts/session_start_hook`, timeout 10) — Host registry/digest; Claude
    performs predecessor/orphan repair, while Codex registers a token and syncs
    an existing managed native agent on startup/resume/clear.
  - `SessionEnd` (→ `scripts/session_end_hook`, timeout 30) — Claude reaps the
    ending session synchronously; Codex starts one detached token-safe reaper.
  - `Stop` (→ `scripts/stop_hook`, timeout 10) — non-blocking running-Jobs notice.
  - `UserPromptSubmit` (→ `scripts/user_prompt_hook`, timeout 10) — inject the
    live-Bridge roster + explicit-addressing routing rule into main.
  - `PreToolUse` (matcher `Bash` → `scripts/bridge_contract_hook`, timeout 10) —
    re-inject the Bridge classification contract into the chinamax Bridge only;
    a second Host-filtered matcher invokes `scripts/codex_pretool_hook` as the
    Codex yolo mutation backstop and is a strict Claude no-op.
  - `SubagentStart` (matcher `chinamax_bridge|chinamax[-_]` →
    `scripts/bridge_contract_hook`, timeout 10) — load the canonical Bridge
    contract when the Host exposes a safe Bridge identity.
  The top-level `description` records the same rationale (JSON has no comments).
