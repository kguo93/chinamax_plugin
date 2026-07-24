---
description: Diagnose the chinamax install in one pass — the conda env, its dependencies, the API-key entries per Profile, and state-dir writability — and record the resolved interpreter.
argument-hint: "[--json] [--workspace <dir>]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamax:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" setup "$ARGUMENTS"`

Present the diagnosis above to the operator VERBATIM. It reports, in one pass:
whether the `chinamax` conda env exists (with the exact `conda create` /
`pip install -e` commands to create it when it does not); whether `chinamax`,
`anthropic`, and `pytest` import under that env; each Profile's API-key entry as
PRESENT or MISSING (never the value); and whether the per-workspace state
directory is writable. Do not summarize — the operator acts on the specific
lines.
