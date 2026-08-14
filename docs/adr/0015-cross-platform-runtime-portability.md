# ADR 0015: Cross-platform Runtime portability

- Status: Accepted
- Date: 2026-08-10
- Target release: 0.4.3

## Context

ChinamaX's durable Runtime was written on Linux and contained implicit Unix
assumptions: `/proc` process inspection, `fcntl` locks, POSIX session creation,
process-group signals, POSIX file modes, XDG state roots, and Bash-oriented
interpreter shims. Both Claude Code and Codex must work on native macOS and
native Windows while Linux behavior remains unchanged.

## Decision

### Platform is orthogonal to Host

`Host` remains `claude` or `codex` and selects adapter, configuration namespace,
hook input, and Host-owned paths. `Platform` is Linux, macOS, or Windows and
selects only native path, process, lock, permission, and launcher mechanisms.
Jobs, Threads, providers, confinement, result payloads, and exit semantics are
Host-neutral and Platform-neutral.

### Target matrix

Latest stable macOS on Apple Silicon and Windows 11 x64 are release targets.
Intel macOS, Windows ARM64, and older releases receive best effort through
generic APIs. WSL2 is a supported Linux alternative; WSL1 is not a Codex target.

### Native Windows plus Git Bash

Windows is implemented natively, with Git Bash required for every existing Bash
shim and Runtime Bash command. The Bash grammar and confinement lexer are not
ported to PowerShell or CMD. Hooks use Codex's Windows-specific command field to
enter `bash`; Claude's shell-form hooks continue through Git Bash.

**Amended 2026-08-11.** Git Bash prerequisite detection now probes the default
Git for Windows install locations before `PATH`, mirroring the conda resolver
(see ADR 0009's 2026-08-11 amendment). The same seam extends to the Codex hooks:
each `commandWindows` now enters Git Bash through `scripts/codex_hook_bash.cmd`
(prefixed `cmd /d /c`) instead of a bare `bash -lc`. The recommended Git for
Windows install leaves `bash.exe` off `PATH`, so a bare `bash` never started even
when detection passed. The launcher resolves `bash.exe` in the default Git for
Windows install roots FIRST (mirroring `doctor._git_for_windows_roots` order and
`_GIT_FOR_WINDOWS_EXES["bash"]`), then on `PATH` as a fallback — root-first so a
stray WSL `bash.exe` on `PATH` cannot shadow the real Git Bash. When nothing
resolves it prints an explicit stderr diagnostic and exits 127. The `.cmd` uses
block-free `if` lines (no `for`-IN set, no `( )` blocks) so the literal `(x86)`
parens never enter a parenthesized construct, and is stored with CRLF via
`.gitattributes` for cmd.exe. A lockstep test pins the launcher's mirror and
root order because batch cannot reuse the Python probe. This reconciles the
"Hook registration" claim below: Codex handlers enter Git Bash through the
launcher, not a direct `bash` invocation. Consciously-accepted new failure mode:
`commandWindows` locates the launcher via `%PLUGIN_ROOT%`, which cmd expands
before any bash exists, so it depends on Codex exporting native `PLUGIN_ROOT`;
there is no `CLAUDE_PLUGIN_ROOT` alias fallback for locating the `.cmd` itself
(the bash payload keeps `${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}` only for the
shim root). The extra `cmd` layer stays within Codex's 3 s `SessionEnd` clamp;
CI runs no Windows runner, so the cmd→bash seam is manual-smoke evidence only,
per the Validation scope below.

**Amended 2026-08-14 (0.4.7).** The 2026-08-11 amendment fixed the Codex hook
launcher but left a second bare-`bash` path unfixed: the Runtime bash TOOL (ADR
0005) spawned `["bash", "-c", command]` relying on `PATH`, diverging from setup's
install-root probe. Because the recommended Git for Windows install leaves
`bash.exe` OFF `PATH`, setup's prerequisite probe could report bash PRESENT
(green) while every in-Job bash spawn failed with `FileNotFoundError` — the two
resolvers disagreed. Resolution: the Git for Windows tool tables and the
root-first probe move to `state` as one shared resolver, `state.windows_tool_path`
(backed by `state.GIT_FOR_WINDOWS_EXES` and `state._git_for_windows_roots` — the
roots helper the 2026-08-11 amendment above names as `doctor._git_for_windows_roots`
now lives in `state`, while `doctor` keeps only the alias
`_GIT_FOR_WINDOWS_EXES = state.GIT_FOR_WINDOWS_EXES` for its remaining
`prerequisite_status` use). That single resolver now
backs BOTH the doctor prerequisite probe AND the Runtime bash spawn, so the check
can never drift from what launches again; the spawn falls back to bare `"bash"`
only when no Git for Windows root resolves. This is a gap-fill, not a reversal.
Validation stays mocked on Linux (`sys.platform` monkeypatched to `win32`, the
resolver and the spawn's `argv[0]` asserted), consistent with the Validation
scope below.

### Process mechanisms

- Linux keeps `/proc`, integer kernel start times, POSIX process groups, and
  `start_new_session=True`.
- macOS uses `psutil` for process identity, ancestry, liveness, and enumeration,
  while retaining native POSIX process groups/signals and `fcntl` locks.
- Windows uses `psutil` for process identity, ancestry, liveness, and recursive
  tree snapshots. Workers and detached Codex reapers use
  `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`; Runtime Bash uses
  `CREATE_NEW_PROCESS_GROUP` while retaining its pipes. Windows termination is a
  start-time-checked terminate/wait/kill/rescan sweep.

PID start times remain integer `pidStartTime` values. `psutil.create_time()` is
normalized to integer microseconds. `NoSuchProcess` and zombies are gone;
`AccessDenied` is alive/unknown; identity mismatch is PID reuse and is never
signalled.

### Lock mechanisms

Linux and macOS retain the current sidecar paths and `fcntl.flock` scopes.
Windows uses `filelock.FileLock` on those same sidecar paths. State and session
locks block; the reaper lock remains non-blocking. Neither conditional package
is imported or installed on Linux.

### Native roots and permissions

Host-provided plugin data remains highest priority. Linux XDG behavior is
unchanged. Fresh macOS installations use `~/Library/Application Support/` and
fresh Windows installations use `%LOCALAPPDATA%` with a
`%USERPROFILE%\\AppData\\Local` fallback. No migration or alternate-root
dual-read behavior is included. POSIX mode guarantees remain on Linux/macOS;
Windows inherits user-owned ACLs and does not call `icacls`.

**Amended 2026-08-10.** The original decision specified the native macOS root
(`~/Library/Application Support/`) at the `host.py` seam but left the shell interpreter
shim (`scripts/_interpreter.sh`) detecting only Windows (via `$OS`); on macOS its
`chinamax_data_root` fell through to the XDG / `~/.local/state` branch and diverged from
`host.py` whenever no Host exported `PLUGIN_DATA`/`CLAUDE_PLUGIN_DATA`. The shim now also
detects macOS with `uname -s = Darwin` (`chinamax_macos`) and routes the fallback data root to
`~/Library/Application Support/{chinamax,chinamax-codex}`, ignoring `XDG_STATE_HOME`, so the
shell and `host.py` agree on every Platform. Only `chinamax_data_root` changed;
`chinamax_resolve_python`/`chinamax_exec` already resolve macOS through their POSIX branch.
Validation stays mocked on Linux: a fake `uname` on `PATH` exercises the branch.

### Conditional dependencies

`psutil>=7.2,<8` is selected on macOS and Windows. `filelock>=3.20,<4` is
selected on Windows only. Existing Linux dependencies and import behavior are
unchanged.

### Hook registration

Claude keeps `hooks/hooks.json`. Codex uses `hooks/codex-hooks.json`, selected by
`.codex-plugin/plugin.json`. The two registrations have parity of events,
matchers, ordering, and stdin; timeout parity preserves each Host's contract,
including Codex's honest 3-second `SessionEnd` value under its CLI clamp. Every
Codex handler has a `commandWindows` that enters Git Bash through
`scripts/codex_hook_bash.cmd` (default Git for Windows install roots → `PATH`),
whose bash payload converts native drive/UNC roots with `cygpath` and quotes the
plugin root (see the 2026-08-11 amendment above).

### Validation scope

The full Linux suite is native regression evidence. macOS and Windows process,
lock, path, setup, launcher, and hook branches are mocked deterministically in
the Linux test environment. No native-OS CI workflow is added in 0.4.3, and
documentation must not claim live native validation.

**Amended 2026-08-12 (0.4.5).** This narrowly reverses the Context claim above
that "Both Claude Code and Codex must work on native macOS and native Windows
while Linux behavior remains unchanged." — for Linux SETUP only. Linux setup now
gates on the `bash` and `miniconda` Prerequisites, and its setup REPORT gains a
`prerequisites` map plus (when something is missing) `prerequisite_fixes`
Rectification rows (see ADR 0009's 2026-08-12 amendment). The reversal is scoped
strictly to setup diagnosis: the Process mechanisms (`/proc`, POSIX process
groups), the XDG state roots, and "Existing Linux dependencies and import behavior
are unchanged" all remain TRUE — setup Prerequisites are external Platform tools,
not the env's Python deps/imports, so none of those Linux runtime guarantees
change. Windows Prerequisite rectification is winget-primary with an
elevation-free per-user `/CURRENTUSER` PowerShell fallback, both landing in the
already-probed Git-for-Windows / Miniconda roots. Zero-state bootstrap (bash and
python both absent) is a skill-relayed set of native Windows commands only; no new
launcher script is added. Validation stays mocked for macOS/Windows per the
Validation scope below; the only live evidence for the new flow is one in-session
Linux smoke of the emitted Miniconda install commands against a throwaway prefix.

## Rejected alternatives

- A PowerShell/CMD Runtime port would change the established Bash grammar and
  confinement semantics.
- WSL-only Windows support would not satisfy native Claude and Codex behavior.
- Unconditional `psutil`/`filelock` would change Linux installation behavior.
- Replacing Unix `fcntl` would add unnecessary Linux churn.
- `taskkill` without start-time identity checks could kill a reused PID.
- `icacls` would mutate user ACLs outside ChinamaX's scope.
- Migrating or dual-reading experimental roots would risk Host/data leakage.
- A broad platform abstraction layer would increase changes without deepening a
  stable interface; the implementation stays at existing seams.
