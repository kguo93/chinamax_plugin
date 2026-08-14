# Official anthropic SDK in a dedicated conda env

**Amended 2026-08-06.** The same `chinamax` environment is used by both Host
adapters; `tomlkit` is added for lossless Codex TOML setup edits.

The Runtime is Python 3.12 in a fresh conda env `chinamax` (never the shared py_automation env) and speaks to every Profile through the official `anthropic` SDK pointed at the profile's base URL — native streaming and tool-use blocks instead of ~300 lines of hand-rolled wire/SSE code. Compatibility is considered proven because the same endpoints already serve Claude Code itself in the implement-handoff skill.

**Amended 2026-08-01**: the `anthropic` floor in `pyproject.toml` rises `>=0.37` → `>=0.118`. The 2026-08-01 reasoning round depends on the `thinking` kwarg on `Messages.stream` and the `thinking_delta`/`signature_delta` stream accumulation, both absent at 0.37 and verified at 0.118.0; without the bump an environment whose already-installed older SDK satisfies `>=0.37` passes setup and then fails at dispatch. The env/SDK decision itself is unchanged. Cross-reference ADR 0001's 2026-08-01 reasoning amendment.

## Portability amendment (0.4.3)

On macOS and Windows the doctor selects conditional runtime dependencies and
probes standard Miniconda locations before `PATH` (including the Windows
per-user install). Linux's existing resolver and dependency behavior remain
unchanged. New-platform setup diagnoses Bash/Git (and Windows `cygpath`) before
mutating an environment.

**Amended 2026-08-11.** Windows prerequisite detection no longer resolves
`git`/`bash`/`cygpath` on `PATH` only. The recommended Git for Windows installer
adds only `\cmd` (which holds `git.exe`) to `PATH`, leaving `bash.exe`
(`\bin`, `\usr\bin`) and `cygpath.exe` (`\usr\bin` only) off `PATH`, so a correct
install failed the old check. Setup now probes the default Git for Windows
install tree on disk first — system-wide `%ProgramFiles%\Git` and per-user
`%LocalAppData%\Programs\Git` (also `%ProgramW6432%\Git` and
`%ProgramFiles(x86)%\Git`), checking `cmd`/`bin`/`usr/bin`/`mingw64/bin` as
applicable per tool — and falls back to `PATH` (union). A miss advises installing
Git for Windows from `https://git-scm.com/download/win` (one installer provides
all three). macOS keeps `PATH` resolution — where its installers place bash/git
by design — with Homebrew / Xcode CLT install advice; Linux stays a no-op.
Detection is table-driven so each Platform is one readable clause. This also
narrows the off-matrix behavior: the old code probed bash/git on ANY non-Linux
platform, whereas `prerequisite_status()` now returns `{}` for any platform that
is not `win32`/`darwin` — platforms outside the ADR 0015 target matrix get no
prerequisite checks (running Windows/macOS-shaped probes there would be wrong).

**Amended 2026-08-12 (0.4.5).** The Prerequisite set and the missing-Prerequisite
flow change. The per-Platform matrix is now Linux → `bash`, `miniconda`; macOS →
`bash`, `miniconda` (git DROPPED from the darwin probe); Windows → `git`, `bash`,
`cygpath`, `miniconda`. Miniconda is a first-class Prerequisite everywhere, probed
"conda resolvable" (`~/miniconda3` first, then `PATH` `conda`) so an existing
anaconda/miniforge conda counts as present. The doctor resolves conda by absolute
path through `_find_conda()` (`~/miniconda3` → `PATH`), PREPENDED to — never
collapsing — `_find_env_python()`'s candidate list so the bare-`conda` `PATH`
fallback survives a machine whose `chinamax` env lives under a different conda, and
`_create_env()` runs that resolved conda.

Setup no longer installs Prerequisites itself. When one is missing, `diagnose()`
emits a structured Rectification-command table (`prerequisite_fixes`) — one row per
missing tool carrying `run_policy` (`agent`/`privileged`/`operator`), `shell`, an
`install_location` display template, and the exact command lines — and `run_setup`
exits without running a fixer. The Host agent installs them ONLY after the operator
types "approve"; Python never installs a Prerequisite and never elevates. `conda
init` runs only inside a miniconda Rectification list, i.e. only on a fresh install
setup itself caused (shells: Linux `bash`; macOS `bash zsh`; Windows `cmd.exe
powershell bash`); a pre-existing conda is never re-initialized.

Miniconda is fetched as `Miniconda3-latest-*` from
`https://repo.anaconda.com/miniconda/` over HTTPS, with NO version pin and NO
checksum. Considered and DECLINED (operator's explicit decision): pinning a specific
Miniconda version and verifying its published SHA256 before running the installer —
the rejected alternative would harden against a mutated `latest` or a corrupted
download, but at the cost of a hardcoded version that ages out and a second network
fetch; the operator accepted the residual risk to keep the emitted commands short
and always current. Windows uses `winget install --id Git.Git` (one UAC click) with
an elevation-free per-user PowerShell fallback (`/CURRENTUSER` into
`%LocalAppData%\Programs\Git`) when winget is absent; Linux bash uses the detected
package manager (auto-run only under passwordless `sudo -n true`, else operator-run);
macOS bash uses `brew install bash` when brew exists, else operator advice.

**Amended 2026-08-14 (0.4.8).** The bare-`python3` bootstrap rung (rung 5) that
lets the doctor start on a machine with no `chinamax` env previously dead-ended on
macOS. Apple ships no real Python 3: `/usr/bin/python3` is an Xcode Command Line
Tools *stub*, not an interpreter, so the rung would `exec` a stub — which can even
pop the CLT GUI installer — and the doctor could never diagnose the interpreter it
needs to start (a bootstrap circularity). `scripts/_interpreter.sh`'s `chinamax_exec`
now inserts a macOS guard after the `~/miniconda3` bootstrap branch and before the
final bare-`python3` `exec`: if no real `python3` is resolvable, it refuses to
`exec`, prints install guidance (Miniconda / Homebrew / python.org), and exits 1;
setup/doctor proceed only once a real `python3` is on `PATH`. Detection is
GUI-safe — it never *executes* `python3` (running the stub is what triggers the
installer) — using `command -v` plus `xcode-select -p`: a non-stub python3
(Homebrew/python.org/Miniconda) resolves off `/usr/bin/python3` and is accepted; the
Apple stub at `/usr/bin/python3` is accepted only when the CLT is installed
(`xcode-select -p` succeeds); nothing resolvable is rejected. Linux always has a real
`python3` and Windows keeps its native-fallback bootstrap (the Windows-only cmd.exe
block in `commands/setup.md`), so both are unchanged. Cross-reference ADR 0015.
Validation stays mocked on Linux: a fake `uname`→Darwin plus `xcode-select`/`conda`
stubs on `PATH` exercise both the refusal and the accept path.
