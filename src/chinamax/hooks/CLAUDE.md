# src/chinamax/hooks/ — conventions

Inventory lives in `./repo-map.md`. Domain vocabulary lives in `../../../CONTEXT.md`.

- **Every hook reads Job state through the ONE shared seam `state.list_jobs_tolerant`**
  (or the sibling `state.iter_workspace_stores` for the cross-workspace reap/roster
  scans) — the same tolerant enumeration `status`'s `load_records` path uses. Never
  hand-roll record parsing in a hook: a malformed `jobs/<id>.json` is skipped and
  counted, and a missing/truncated `state.json` is rebuilt from the record files.
- **A whole-hook failure degrades to exit 0 with EMPTY stdout; diagnostics go to
  stderr, never stdout.** No traceback may land in Claude's context. The digest is
  built in full BEFORE it is written, so a mid-build exception leaves stdout empty.
- **Jobs are session-scoped (ADR 0004, reversed 2026-07-30).** SessionStart writes
  the session-liveness registry FIRST, then reaps a same-PID predecessor, then runs
  `reap_orphans`; SessionEnd reaps THEN removes the registry (reap first,
  registry-removal last — a hook killed mid-reap degrades to the next SessionStart's
  orphan path, which is idempotent). Reaps run with the short
  `state.SESSION_REAP_GRACE_S`/`SESSION_REAP_CONFIRM_S`, not `cancel`'s 10 s + 5 s.
- **`interrupted` is BOTH read-side-derived AND a stored status now.** SessionStart's
  digest lists active + derived/stored-interrupted + recent-finished; Stop lists
  ACTIVE ONLY. Both grade through `state.effective_status`, never a re-derived
  liveness check. A DERIVED-interrupted seed is a dead pid + aged `updatedAt` over a
  stored `running`; a reaped STORED-interrupted record is written by `reap_orphans`.
- **`resolve_owner_process` records the LONG-LIVED Claude process, never the hook
  runner.** It walks the `/proc` ancestor chain from `os.getppid()`, skipping login
  shells and the plugin's own `scripts/` shims — recording a transient runner pid
  would make every session read dead and turn the orphan reaper into a live-Job
  killer. Session liveness is tested with the `worker_gone` family (ESRCH/zombie/
  start-time), never a bare `os.kill(pid, 0)`.
- **The bridge_contract hook is REINFORCEMENT, never a gate (ADR 0010).** It filters
  on `agent_type == "chinamax:chinamax"` and injects `additionalContext`; it never
  emits a `decision`/deny. The `CONTRACT` constant is the single source of the
  injected text (the test imports it); do not fork a second copy in the module body.
- **Channels are fixed:** SessionStart → the digest to STDOUT and the exports to
  `$CLAUDE_ENV_FILE` (shell-quoted with `shlex.quote`); Stop and the two injection
  hooks → a single JSON object on STDOUT (`systemMessage` for Stop; a
  `hookSpecificOutput.additionalContext` envelope for UserPromptSubmit/PreToolUse).
  Stop never writes a `decision` key — a Stop hook must never block a turn.
- **`CLAUDE_PLUGIN_DATA` is re-exported for a reason:** the hooks and the Bridge's
  dispatches must agree on the state root, or Jobs land in one root while the digest,
  the registry and the reaps read another.
