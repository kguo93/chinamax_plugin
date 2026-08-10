# ChinamaX

ChinamaX dispatches durable worker-model Jobs through one shared Runtime. The
same Job verbs, provider protocol, Bridge contract, result format, and lifecycle
behavior work from Claude Code and Codex on Linux, macOS, and native Windows.

## Support matrix

| Platform | Claude Code | Codex | Release target |
|---|---:|---:|---|
| Linux | Yes | Yes | Existing behavior preserved |
| Latest stable macOS, Apple Silicon | Yes | Yes | Supported |
| Windows 11, x64 | Yes | Yes | Supported; Git Bash required |
| Intel macOS | Best effort | Best effort | Use an Intel-compatible Miniconda release |
| Windows ARM64 | Best effort | Best effort | Generic Python/Conda paths only |
| Older OS releases | Best effort | Best effort | Documented only; no legacy branches |
| WSL2 | Linux path | Linux path | Alternative Windows installation |
| WSL1 | — | No | Unsupported for Codex |

Windows support is native Windows plus Git Bash. ChinamaX Runtime Bash commands
remain Bash commands; they are not translated to PowerShell or CMD. Codex's
native Windows sandbox and WSL2 are separate installation paths; WSL2 is not
required for the native Windows target.

## Prerequisites

All Platforms need a current Claude Code or Codex installation, Git, a writable
user data directory, and Python 3.12 supplied by Miniconda or an equivalent
Conda environment.

### Linux

Use the existing Linux setup. `bash`, `git`, and `conda` must be available using
the same paths your current ChinamaX installation uses.

### macOS

Install Git and Bash, then install Miniconda for your architecture. The latest
Apple Silicon release is the supported target; Intel is best effort. ChinamaX
adds `psutil` to the managed environment on macOS.

### Windows

Install Git for Windows and ensure `bash.exe`, `git.exe`, and `cygpath.exe` are
discoverable. Git Bash is the required shell for all ChinamaX shims and hooks.
Install Miniconda for Windows x64 (the default per-user “Just Me” location is
probed even when Conda is not on `PATH`). ChinamaX adds `psutil` and `filelock`
to the managed environment on Windows.

Do not install WSL, PowerShell modules, `taskkill`, or `icacls` for ChinamaX.

## Installation

### Claude Code

```bash
claude plugin marketplace add kguo93/chinamax_plugin
claude plugin install chinamax@chinamax-plugin
```

### Codex

```bash
codex plugin marketplace add kguo93/chinamax_plugin
codex plugin add chinamax@chinamax-plugin
```

Codex hook configuration is in `hooks/codex-hooks.json`; Claude hook
configuration remains in `hooks/hooks.json`. Approve/trust the Codex hooks when
the Host asks. On Windows, Claude shell-form hooks and Codex `commandWindows`
handlers enter Git Bash explicitly.

## Setup

Run setup once per Host. Setup creates or locates the `chinamax` Conda
environment, installs the package and its conditional dependencies, records the
selected interpreter, scaffolds a commented key template, and checks profiles.

- Claude: `/chinamax:setup`
- Codex: `$chinamax-setup` under `codex --yolo` when applying mutations

Codex setup is preview-first and content-addressed. A preview is non-mutating;
apply only the exact consent digest from that preview. The yolo permission is
used only for Codex setup mutation. Runtime `--read-only` remains ChinamaX's
tool-layer policy and is not replaced by a Host sandbox.

On macOS and Windows, setup diagnoses and refuses to mutate when a required
external prerequisite is missing. Install the named tool and rerun setup. Setup
does not download installers or modify system `PATH`.

## Keys and native data paths

Add one `NAME=value` line per provider key. Values never appear in diagnostics.

| Host | Configuration/keys | Linux fallback state | macOS fallback state | Windows fallback state |
|---|---|---|---|---|
| Claude | `~/.claude/model-keys.env`, `~/.claude/chinamax-profiles.json` | `$XDG_STATE_HOME/chinamax` or `~/.local/state/chinamax` | `~/Library/Application Support/chinamax` | `%LOCALAPPDATA%\\chinamax` |
| Codex | `~/.codex/model-keys.env`, `~/.codex/chinamax-profiles.json` | `$XDG_STATE_HOME/chinamax-codex` or `~/.local/state/chinamax-codex` | `~/Library/Application Support/chinamax-codex` | `%LOCALAPPDATA%\\chinamax-codex` |

Host-provided plugin-data variables always take precedence:

- Claude: `CLAUDE_PLUGIN_DATA`
- Codex: `PLUGIN_DATA`

On Windows, `%USERPROFILE%\\AppData\\Local` is the fallback when
`%LOCALAPPDATA%` is absent. A Git Bash `HOME=/c/Users/...` does not override a
native `USERPROFILE`. Claude and Codex roots never cross.

The 0.4.3 macOS and Windows implementation assumes a fresh install. It does
not read, merge, or migrate experimental state from another location. Existing
Linux state and Linux XDG behavior remain unchanged.

POSIX directories/files are still created with `0700`/`0600` on Linux and
macOS. Windows inherits the ACLs of the user-owned parent directory; ChinamaX
does not rewrite ACLs with `icacls`.

## Use

`profile=` is required and the Runtime command grammar is identical on every
Platform.

```text
Claude: /chinamax:task profile=deepseek summarize these logs
Codex:  $chinamax-task profile=deepseek summarize these logs
```

Useful operations:

```text
/chinamax:status                 # Codex: $chinamax-status
/chinamax:profiles               # Codex: $chinamax-profiles
/chinamax:task profile=kimi --read-only explain the auth flow
```

The Bridge dispatches one durable Job, polls it, and relays exactly one worker
response. Steer, resume, and cancel semantics are unchanged across Hosts and
Platforms. Prompt/stdin transport remains quoted and Bash-oriented, including
paths with spaces.

## Troubleshooting

- **`bash` not found:** install Bash; on Windows install Git for Windows and put
  its Bash executable on `PATH`.
- **`cygpath` not found on Windows:** add Git for Windows' `usr/bin` directory to
  `PATH`, then rerun setup.
- **`git` not found:** install Git and ensure `git` is on `PATH`; workspace-root
  discovery requires it on new Platforms.
- **Conda/Python not found:** install Miniconda, or set `CHINAMAX_PYTHON` to an
  absolute executable. Standard `~/miniconda3` and Windows per-user paths are
  probed before `PATH`.
- **`psutil` or `filelock` missing:** rerun setup in the managed environment;
  `filelock` is Windows-only and `psutil` is macOS/Windows-only.
- **Wrong state root:** inspect `CLAUDE_PLUGIN_DATA`/`PLUGIN_DATA`, then unset a
  stale or relative value. Relative plugin-data values are rejected.
- **Windows path with spaces:** keep the plugin root and data paths quoted; the
  shipped hooks and shims already quote them.
- **Deep Windows state paths:** the default Windows 260-character `MAX_PATH`
  limit can still affect deeply nested workspaces; enable Windows long-path
  support or use a shorter workspace/data root.
- **Cancellation reports survivor PIDs:** a process denied by Windows or still
  alive after the terminate/kill grace is intentionally reported. Retry status or
  cancel after investigating the listed process.
- **Intel Mac or Windows ARM64:** these are best effort. Use a supported
  architecture for release-blocking deployments.
- **Want a Linux environment on Windows:** use WSL2 and follow the Linux path.
  WSL1 is unsupported for Codex.

## Verification scope

The Linux suite is the native regression baseline. macOS and Windows branches
are covered by deterministic mocked process, lock, path, setup, and hook tests in
0.4.3; this release does not claim native macOS/Windows CI or live-host evidence.
