# scripts/ — conventions

Inventory lives in `./repo-map.md`.

- `_interpreter.sh` exports/resolves the Host marker before selecting the
  Host-specific data root. `PLUGIN_*` evidence wins over Claude aliases.
- `codex_pretool_hook` is the Codex-only mutation backstop; preserve the Claude
  no-op and Runtime `--read-only` boundary.

- **Interpreter discovery lives ONLY in `_interpreter.sh`.** The launcher, the
  commands, and the hooks all go through `chinamax_exec`, so they never drift onto
  three different pythons. The Bridge Agent (`agents/chinamax.md`) documents the
  same order in prose — keep the two in lockstep. Do NOT add a second resolution
  path anywhere.
- **The bootstrap rung (system `python3` + `src/` on `PYTHONPATH`) is load-bearing.**
  On a fresh machine with no `chinamax` env — exactly what `/chinamax:setup` exists
  to diagnose — every conda rung fails, so without it the doctor could never start.
  Never delete it as "dead code".
- **The shims are NOT component-scanned by the plugin loader** (only `agents/`,
  `commands/`, and `hooks/hooks.json`, and `skills/*/SKILL.md` are), so this trio
  is safe here — a stray `.md` in `scripts/` registers as nothing.
- **The shims pass argv AND stdin through verbatim.** The CLI does the argv
  normalization (a single quoted `"$ARGUMENTS"` element); the shim must not
  pre-split or reshape it. `set -euo pipefail`, and every env-var read defaults with
  `${VAR:-}` so `set -u` never trips.
- **`chmod +x` the three entrypoints** (`chinamax`, `session_start_hook`,
  `stop_hook`); `_interpreter.sh` is sourced, so it need not be executable.
- **`codex_hook_bash.cmd` locates Git Bash ONLY.** It exists to find `bash.exe`
  before any POSIX shell exists (so it cannot be an sh shim), and must never grow
  interpreter-discovery logic — that stays solely in `_interpreter.sh`.

On native Windows `_interpreter.sh` converts native roots with `cygpath`, probes
the per-user Miniconda installation, and selects Git Bash. On macOS
(`uname -s = Darwin`) it routes the fallback data root to
`~/Library/Application Support/{chinamax,chinamax-codex}`, ignoring `XDG_STATE_HOME`, to
match `host.py`. Keep the Windows/macOS branches narrow and preserve the Linux resolution
order exactly.
