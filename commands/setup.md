---
description: Set up the chinamax install — diagnose Prerequisites (bash, Miniconda, Git for Windows), pause for approval before installing any that are missing, then create the conda env and install the dependencies when missing, scaffold a commented ~/.claude/model-keys.env template, diagnose the API-key entries and state-dir writability, and record the resolved interpreter.
argument-hint: "[--json] [--workspace <dir>]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamax:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" setup "$ARGUMENTS"`

Follow this protocol on the report above. Do not summarize; the operator acts on the specific lines.

1. Read the report the launcher emitted.
2. Exit 0 → present the report verbatim; done.
3. Exit 1 WITH a `prerequisite_fixes` section → show each row: name, summary, install_location, run_policy, commands. Warn that a miniconda row runs `conda init`, which edits shell startup files. Ask exactly: reply "approve" to install these, anything else to stop.
4. Reply is not "approve" → stop. Report that no Prerequisite was installed and no fixer ran. Do NOT claim nothing changed — the earlier diagnose already did its state-probe/interpreter record.
5. Reply is "approve" → for each row IN ORDER, dispatch by `run_policy`, running every command through the row's `shell` (a `cmd` row via `cmd /c`; a `powershell`/`native` row natively — never in Git Bash):
   - `agent` → run its commands.
   - `privileged` → run `sudo -n true`; on success run the commands; on failure ask the operator to run the shown command themselves (type `! <command>` in the prompt), then wait.
   - `operator` → hand the summary to the operator, wait for them to install manually, run none of the (empty) commands.
   Stop-on-first-failure: if any command exits non-zero, run no remaining command in that row (no `conda init`) AND stop the whole flow — attempt no later row, do not re-run. Report the exact failed command and its exit status.
   Each command still triggers the Host's normal permission prompt; the word "approve" is textual consent, not a bypass. Do not widen `allowed-tools`.
6. Re-run the launcher once (a fresh `scripts/chinamax setup` Bash call). Still missing → report and stop. Never loop.
7. Exit 1 WITHOUT a `prerequisite_fixes` section → report the failure rows as before.

Windows-only: if the launcher itself cannot start (bash or python missing), run these natively in cmd.exe, then return to step 1:

```text
winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
curl.exe -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe -o "%TEMP%\chinamax-miniconda.exe"
start /wait "" "%TEMP%\chinamax-miniconda.exe" /InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /S /D=%USERPROFILE%\miniconda3
"%USERPROFILE%\miniconda3\Scripts\conda.exe" init cmd.exe powershell bash
```
