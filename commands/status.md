---
description: Show chinamax Jobs in this workspace — every active one plus the recent finished ones, or one named Job (optionally waiting for it to change).
argument-hint: "[job-id] [--wait] [--timeout-ms <ms>] [--workspace <dir>]"
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamax:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" status "$ARGUMENTS"`

Present the command output above to the operator VERBATIM — it is the Job
listing or the single-Job status with its progress preview. The bare listing
ends with a `policy:` footer showing the three worker toggles (memory / hooks /
mcp), any malformed-settings flag, and the full `settings.json` path. Do not
summarize, re-order, or add commentary; preserve the lines as printed.
