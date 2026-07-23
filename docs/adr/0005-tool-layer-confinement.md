# Confinement is tool-layer, not OS sandbox

File tools hard-reject any realpath outside the workspace (symlink-escape safe); bash runs cwd-pinned with per-command timeouts and a denylist mirroring the operator's global hard-bans; read-only Jobs disable write tools and block write-shaped bash. We rejected OS-level sandboxing (bubblewrap) to stay pure-Python, dependency-free, and fully unit-testable. Residual risk is documented, not defended: a hostile model could still write via bash redirection.
