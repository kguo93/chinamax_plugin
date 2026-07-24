---
description: Steer a running chinamax Job — enqueue a mid-run message that lands in its Thread at the next loop boundary. With no id, the single active Job; several active Jobs are listed rather than guessed.
argument-hint: "[job-id] <steer message>"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamax:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" steer "$ARGUMENTS"`

The steer message is preserved intact (a leading Job id, if given, is peeled
first — transport mirrors `/chinamax:resume`). With no id the single active Job
is targeted; several active Jobs are listed rather than guessed, exactly like
`/chinamax:cancel`. A message is required. A finished or interrupted Job is
refused with a pointer to `resume`, which is the only path that continues its
Thread.

This command enqueues a steer from the main context in-turn, with none of the
latency of the Bridge's long poll — same queue, drained at the runtime's next
loop iteration.

Present the command output above to the operator VERBATIM — the queued steer id,
or the refusal (no active Job, several active Jobs listed, a finished Job, or an
empty message). Do not add commentary, and do not do the task yourself.
