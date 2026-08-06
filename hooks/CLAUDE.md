# hooks/ — conventions

Inventory lives in `./repo-map.md`.

- **`hooks.json` registers lifecycle plus Host enforcement events** (ADR 0004 reversed 2026-07-30 — Jobs are
  now session-scoped): `SessionStart`, `SessionEnd`, `Stop`, `UserPromptSubmit`,
  `PreToolUse` (Claude Bridge matcher `Bash` plus Codex mutation matcher), and
  `SubagentStart` (Bridge contract loader). `SessionEnd` is the reversal's core: a session
  ending — including `/clear` — kills its still-active Jobs, so a Job no longer
  outlives the session that started it.
- **Codex hooks are Host-aware.** They require explicit yolo permission for
  mutating setup/Agent operations, use `PLUGIN_*` roots, and detach a
  token-checked SessionEnd reaper; the SessionStart message carries the token
  needed by every later dispatch. Claude paths and lifecycle semantics remain
  unchanged.
- **The `SessionStart` matcher is `startup|resume|clear|compact|fork`.** `clear`
  re-injects inherited-Job awareness after `/clear` wipes context (and its digest
  now reports what the orphan reap just terminated); `fork` matters because a
  forked session inherits the parent's exported `CHINAMAX_SESSION_ID` — without its
  own SessionStart it never registers its own owner and its dispatches are reaped
  with the parent.
- **Timeouts are 10s, except `SessionEnd` at 30s.** Interpreter resolution can fall
  through to `conda run` (process-startup cost before the hook body), and a
  SessionEnd reap may kill several Jobs; its reap runs with the short
  `state.SESSION_REAP_GRACE_S`/`SESSION_REAP_CONFIRM_S` so it fits that budget.
- **The `PreToolUse(Bash)` hook fires on EVERY Bash call in every session.** Its
  shim fast-paths in-shell: it buffers stdin and, unless the payload contains
  `chinamax`, exits 0 WITHOUT launching python — only a marked event pays the
  interpreter-resolution cost. The python side filters again on the `chinamax`
  substring of `agent_type` (a NAMED spawn puts the teammate name,
  `chinamax-<profile>-<slug>`, in `agent_type`, not the `chinamax:chinamax` subagent
  type) and injects context; it NEVER blocks a call (ADR 0010, no hard blocks).
- **The command strings run the `scripts/` shims, not the python modules directly**
  (`"${CLAUDE_PLUGIN_ROOT}/scripts/session_end_hook"`). The shim resolves the env
  interpreter; naming `python -m …` here would skip that. Keep the hook LOGIC in
  `src/chinamax/hooks/`, not in this directory.
- **Only `hooks.json` is consumed here; the loader does not `.md`-component-scan
  `hooks/`,** so the CLAUDE.md/AGENTS.md/repo-map.md trio is safe.
