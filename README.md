# ChinamaX

ChinamaX dispatches durable worker-model Jobs through one shared Runtime. The
same Job verbs, provider protocol, Bridge contract, result format, and lifecycle
behavior work from Claude Code and Codex on Linux, macOS, and native Windows.

## Support matrix

| Platform | Claude Code | Codex | Release target |
|---|---:|---:|---|
| Linux | Yes | Yes | Runtime preserved; setup adds a prerequisite gate |
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

All Platforms need a current Claude Code or Codex installation, `bash`, a writable
user data directory, and Python 3.12 supplied by Miniconda or an equivalent Conda
environment. Setup treats `bash` and Miniconda (and, on Windows, Git for Windows'
`git`/`bash`/`cygpath`) as Prerequisites: when one is missing it pauses and prints
the exact install commands, installing them only after you approve (see Setup).

### Linux

Use the existing Linux setup. `bash` and `conda` must be available using the same
paths your current ChinamaX installation uses; if Miniconda is absent, setup emits
the install commands and runs them after you approve. Git is not a Linux setup
Prerequisite.

### macOS

Setup probes `bash` and Miniconda on macOS (Git is no longer a macOS setup
Prerequisite, though Git remains useful — `xcode-select --install` for the Xcode
Command Line Tools, or `brew install git`). Bash is preinstalled; `brew install
bash` provides a newer version, which setup offers to run after you approve.
Install Miniconda for your architecture, or let setup install it. The latest
Apple Silicon release is the supported target; Intel is best effort. ChinamaX adds
`psutil` to the managed environment on macOS.

### Windows

Install Git for Windows (https://git-scm.com/download/win). Setup detects
`git.exe`, `bash.exe`, and `cygpath.exe` in the default Git for Windows install
tree even when they are not on `PATH` — the recommended installer adds only
`\cmd` to `PATH`, leaving `bash.exe` and `cygpath.exe` off it. Git Bash is the
required shell for all ChinamaX shims and hooks. Install Miniconda for Windows
x64 (the default per-user “Just Me” location is probed even when Conda is not on
`PATH`). ChinamaX adds `psutil` and `filelock` to the managed environment on
Windows.

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

Run setup once per Host, after install. Setup creates or locates the `chinamax`
Conda environment, installs the package and its conditional dependencies, records
the selected interpreter, scaffolds a commented key template, and checks profiles.

- Claude: `/chinamax:setup`
- Codex: `$chinamax-setup` under `codex --yolo` when applying mutations

When a Prerequisite (`bash`, Miniconda, or Git for Windows) is missing, setup
pauses and lists each missing tool with the exact install commands. Reply
"approve" and the Host agent runs them (a miniconda row runs `conda init`, which
edits your shell startup files; each command still prompts for permission);
anything else stops without installing. Setup itself never installs a Prerequisite
and never elevates. Note that approving a Miniconda (or, on Windows, Git)
Rectification DOES download that installer — so it is no longer true that setup
downloads no installers; it downloads none without your approval.

Codex setup is preview-first and content-addressed. A preview is non-mutating;
apply only the exact consent digest from that preview. The yolo permission is
used only for Codex setup mutation. Runtime `--read-only` remains ChinamaX's
tool-layer policy and is not replaced by a Host sandbox.

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

- **`git`, `bash`, or `cygpath` not found (Windows):** install Git for Windows
  from https://git-scm.com/download/win — one installer provides all three, and
  setup detects them in the default install tree even when they are off `PATH`.
- **`bash` not found (macOS):** approve the `brew install bash` Rectification setup
  prints, or install Homebrew / the Xcode Command Line Tools first. Setup no longer
  probes `git` on macOS (install Git separately if you want it).
- **Conda/Python not found:** approve the Miniconda Rectification setup prints, or
  install Miniconda yourself. Standard `~/miniconda3` and Windows per-user paths are
  probed before `PATH`. Setting `CHINAMAX_PYTHON` to an absolute interpreter does
  NOT skip the Miniconda pause: setup's Phase A keys on conda-resolvability
  regardless of which Python runs the doctor.
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

The Linux suite is the native regression baseline. macOS and Windows branches —
including the 0.4.5 Prerequisite matrix and Rectification-command emission — are
covered by deterministic mocked process, lock, path, setup, prerequisite, and hook
tests; this release does not claim native macOS/Windows CI or live-host evidence.
The only live evidence for the new prerequisite flow is one in-session Linux smoke
of the emitted Miniconda install commands against a throwaway prefix.
