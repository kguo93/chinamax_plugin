---
description: List the configured chinamax Profiles with endpoint, model, and API-key presence (PRESENT/MISSING — never the key value).
argument-hint: ""
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamax:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" profiles "$ARGUMENTS"`

Present the Profile listing above to the operator VERBATIM. It reports each
Profile's endpoint, model, and whether its API key is PRESENT or MISSING — it
never prints a key value, and neither should you.
