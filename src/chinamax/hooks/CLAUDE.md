# src/chinamax/hooks/ — conventions

Inventory lives in `./repo-map.md`. Domain vocabulary lives in `../../../CONTEXT.md`.

- **Both hooks read Job state through the ONE shared seam `state.list_jobs_tolerant`**
  — the same tolerant enumeration `status`'s `load_records` path uses. Never
  hand-roll record parsing in a hook: a malformed `jobs/<id>.json` is skipped and
  counted, and a missing/truncated `state.json` is rebuilt from the record files
  (jobs/01's store-invariants), so the digest never goes silent on corrupt state.
- **A whole-hook failure degrades to exit 0 with EMPTY stdout; diagnostics go to
  stderr, never stdout.** No traceback may land in Claude's context. The digest is
  built in full BEFORE it is written, so a mid-build exception leaves stdout empty.
- **`resolve_workspace` walks to the git toplevel** (three rungs: stdin `cwd` →
  `CLAUDE_PROJECT_DIR` → process cwd), so a session opened in a SUBDIRECTORY of the
  dispatching repo still finds that repo's Jobs — the 70-minute-inheritance case.
- **`interrupted` is READ-SIDE only and never a stored status.** SessionStart lists
  active + interrupted + recent-finished; Stop lists ACTIVE ONLY (interrupted work
  is not in flight). Both grade through `state.effective_status`, never a re-derived
  liveness check. A test seeds `interrupted` with a dead pid + aged `updatedAt`,
  never a stored `status: interrupted`.
- **SessionStart channels are fixed:** the digest to STDOUT (Claude injects it as
  context), the exports to `$CLAUDE_ENV_FILE` (no-op when unset), shell-quoted with
  `shlex.quote` so a session id with a quote or newline round-trips. **Stop** writes
  ONLY `systemMessage` (a non-blocking, operator-visible field) and never a
  `decision` key — a Stop hook must never block a turn.
- **`CLAUDE_PLUGIN_DATA` is re-exported for a reason:** the hooks and the Bridge's
  dispatches must agree on the state root, or Jobs land in one root while the digest
  reads the other.
