---
name: chinamax-setup
description: Preview and consent to Codex Host ChinamaX setup changes.
user-invocable: false
---

Refuse under a Claude Host. Setup mutation requires a trusted Codex status that
normalizes to `permission_mode=bypassPermissions`: either explicit
`bypassPermissions`/`YOLO mode`, or the structured pair
`approval_policy=never` and `sandbox_mode=danger-full-access`. Otherwise tell the
operator to rerun the Host's YOLO launch option (`codex --yolo` or, on current
Codex CLI builds, `--dangerously-bypass-approvals-and-sandbox`), explain that
YOLO disables approval/sandbox enforcement, and identify Runtime `--read-only`
as the worker enforcement boundary.

Run the Codex setup planner first. It must be genuinely non-mutating, show a
redacted content-addressed preview and consent digest, and ask one explicit
yes/no question. When the preview's `config_consequences` is non-empty, show it
verbatim to the operator before the yes/no question — it states that enabling the
three Codex feature flags changes global Codex behavior for all sessions.
Apply only after consent and a matching recomputed digest.
Preserve TOML comments and unrelated config, never copy or print credentials,
never persist yolo, and use the shared deterministic compiler for the managed
`~/.codex/agents/chinamax_bridge.toml`. An unmanaged collision needs a second
overwrite confirmation. A declined agent install warns but does not disable
dynamic Terra/low spawning.

The three worker Policy toggles (memory / hooks / mcp, each default OFF) ride the
preview and consent digest — not a per-toggle prompt. The preview's `policy`
block shows the values that WILL be written (explicit `memory=on|off
hooks=on|off mcp=on|off` args, else the current or OFF defaults); the digest
folds BOTH the observed current `settings.json` and the proposed values, so a
hand-edit between preview and apply aborts. Apply writes `settings.json` and
lists it in `changed`. Pass the same toggle args on the apply as on the preview.

Prerequisites first. The Phase A preview carries a `prerequisite_fixes` array —
one row per missing bash / Miniconda / Git for Windows Prerequisite. When it is
non-empty, act BEFORE any consent digest:

1. Show each row: name, summary, install_location, run_policy, commands. Warn
   that a miniconda row runs `conda init`, which edits shell startup files.
2. Ask the operator to reply "approve" to install these, anything else to stop.
3. Not "approve" → stop; no Prerequisite installed and no config applied.
4. "approve" → for each row IN ORDER, dispatch by `run_policy`, each command
   through the row's `shell` (a `cmd` row via `cmd /c`; a `powershell`/`native`
   row natively — never Git Bash):
   - `agent` → run its commands.
   - `privileged` → run `sudo -n true`; success → run; failure → have the
     operator run the shown command themselves.
   - `operator` → hand the summary to the operator; run none of the empty commands.
   Stop-on-first-failure: a non-zero command runs no remaining command in that
   row (no `conda init`) and stops the whole flow; report the failed command.
5. Re-run the preview (the digest changed), then proceed with the fresh digest.

The CLI seam is `CHINAMAX_HOST=codex scripts/chinamax setup --json` for Phase A.
After an affirmative answer and any prerequisite install, rerun the preview, then
use `CHINAMAX_HOST=codex CODEX_PERMISSION_MODE=bypassPermissions scripts/chinamax
setup --apply --consent-digest <digest> --json`; add `--confirm-overwrite` only
after the separate unmanaged-file confirmation. A stale digest aborts without
applying. `--apply` keeps refusing while any Prerequisite is still missing.

On native Windows, run the `scripts/chinamax` SEAM invocation through Git Bash
(`shell: bash`) and quote `$PLUGIN_ROOT/scripts/chinamax`; do not rewrite THAT
command as PowerShell or CMD. This does NOT forbid the `prerequisite_fixes` rows,
which by design run via `cmd /c` (`shell: cmd`) or natively (`shell:
powershell`/`native`). `bash`, `git`, `cygpath`, and Miniconda are prerequisites;
setup detects them (Git for Windows in its default install tree, not merely
`PATH`) and emits Rectification rows before mutating files.

Windows-only: if the seam itself cannot start (bash or python missing), run these
natively in cmd.exe, then return to Phase A:

```text
winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
curl.exe -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe -o "%TEMP%\chinamax-miniconda.exe"
start /wait "" "%TEMP%\chinamax-miniconda.exe" /InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /S /D=%USERPROFILE%\miniconda3
"%USERPROFILE%\miniconda3\Scripts\conda.exe" init cmd.exe powershell bash
```
