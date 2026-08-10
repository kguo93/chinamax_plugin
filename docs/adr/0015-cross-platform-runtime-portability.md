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
Codex handler has a `commandWindows` that invokes Git Bash, converts native
drive/UNC roots with `cygpath`, and quotes the plugin root.

### Validation scope

The full Linux suite is native regression evidence. macOS and Windows process,
lock, path, setup, launcher, and hook branches are mocked deterministically in
the Linux test environment. No native-OS CI workflow is added in 0.4.3, and
documentation must not claim live native validation.

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
