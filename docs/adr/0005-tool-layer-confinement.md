# Confinement is tool-layer, not OS sandbox

**Amended 2026-08-06.** Codex yolo is required for mutating adapter operations,
but Runtime `--read-only` remains the authoritative tool-layer policy; Codex's
sandbox is not treated as a substitute for Runtime confinement.

File tools hard-reject any realpath outside the workspace (symlink-escape safe); bash runs cwd-pinned with per-command timeouts and a denylist mirroring the operator's global hard-bans; read-only Jobs disable write tools and block write-shaped bash. We rejected OS-level sandboxing (bubblewrap) to stay pure-Python, dependency-free, and fully unit-testable. Residual risk is documented, not defended: a hostile model could still write via bash redirection.

## Portability amendment (0.4.3)

The portability dependencies (`psutil` and Windows-only `filelock`) implement
lifecycle and locking mechanisms, not confinement. Runtime Bash remains
`bash -c` with the existing POSIX lexer and tool-layer confinement on every
Platform, including native Windows through Git Bash.

**Amended 2026-08-14 (0.4.7).** The Runtime bash tool's Windows executable
resolution is governed by ADR 0015: on native Windows the `bash -c` spawn now
resolves `bash.exe` through the shared `state.windows_tool_path` (Git for Windows
install-root probe first, then `PATH`) instead of a bare `bash` that assumed
`PATH`. Confinement itself is unchanged — still cwd-pinned `bash -c` with the
denylist and per-command timeouts on every Platform; only *which* `bash.exe`
launches changed. See ADR 0015's 2026-08-14 amendment.

**Amended 2026-08-15 (0.5.0).** Worker Host-policy enforcement (ADR 0016) adds a
SECOND gate that composes AHEAD of this tool-layer confinement without weakening
it. A PreToolUse Policy hook may DENY a tool call before it reaches dispatch, but
a Policy-hook ALLOW can never LIFT confinement: the denylist, the read-only
write-shaped refusal, and realpath containment remain the floor a hook allow
cannot raise. Hook commands themselves are operator-authored host policy and sit
OUTSIDE the tool boundary — they are NOT lexed/confined by this module and run
even for `--read-only` Jobs. Separately, **Worker MCP** tools stay advertised and
callable in read-only Jobs (outside the tool-layer posture, the same class as the
bash-redirection network-egress residual documented above), and MCP server
processes run UNSANDBOXED with full host filesystem/network capability regardless
of Job posture — distinct from "tool omitted from the model". See ADR 0016.

**Amended 2026-08-16 (0.6.0).** Containment gains a second permitted root, the
**Scratch root** — the realpath of the Platform temp directory (Python
`tempfile.gettempdir()`, honoring `TMPDIR`/`%TEMP%`; deliberately NOT literal
`/tmp`, which on macOS would miss `$TMPDIR` scratch), computed once at module
import. Purpose of record: a scratchpad escape hatch for workers. File-tool
containment now accepts a realpath under the workspace OR the Scratch root,
uniformly at direct resolution (`resolve_in_workspace`) and walk re-validation
(`contained`); the WHOLE temp directory is carved out — per-Thread/Job subtrees
were rejected (lifecycle machinery; a resumed Thread's new Job would orphan its
scratch). Everything else stands: bash is untouched (cwd stays workspace-pinned
with the denylist, and read-only Jobs still refuse every file-targeting
redirect, Scratch-root-aimed included — target-aware gating of lexed, unexpanded
shell words was rejected as unreliable by construction); read-only Jobs still
carry no write tools — the carve-out widens only where their read tools may
look. The system prompt names the resolved Scratch root per Job. Containment
stays component-wise (sibling-prefix safe) against both roots.
