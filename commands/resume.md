---
description: Continue a finished chinamax Job's Thread with a follow-up prompt — a new Job that inherits the source's Profile and write posture. Optionally name the source Job id first.
argument-hint: "[job-id] <follow-up prompt>"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamax:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" resume "$ARGUMENTS"`

The follow-up prompt is preserved intact (a leading Job id, if given, is peeled
first). `resume` continues the prior Thread and takes no Profile — it inherits
one. It refuses while any Job in this workspace is still active.

Present the command output above to the operator VERBATIM: the new Job id on a
successful resume, or the refusal when a Job is still running. Do not do the
task yourself.
