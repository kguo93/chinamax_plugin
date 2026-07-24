---
description: Stop a running chinamax Job — kill its whole process tree and mark the record cancelled. With no id, the single active Job; several active Jobs are listed rather than guessed.
argument-hint: "[job-id] [--workspace <dir>]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamax:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" cancel "$ARGUMENTS"`

Present the command output above to the operator VERBATIM — it names which Job
was cancelled, or lists the active Jobs when the target was ambiguous, or
reports that nothing was cancellable. Do not add commentary.
