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
