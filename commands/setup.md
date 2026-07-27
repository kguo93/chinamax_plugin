---
description: Set up the chinamax install in one pass — create the conda env and install the dependencies when missing, scaffold a commented ~/.claude/model-keys.env template, diagnose the API-key entries and state-dir writability, and record the resolved interpreter.
argument-hint: "[--json] [--workspace <dir>]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/chinamax:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" setup "$ARGUMENTS"`

Present the report above to the operator VERBATIM. In one pass it diagnoses AND
fixes what a first run needs: a missing `chinamax` conda env is created
(`conda create -y -n chinamax python=3.12`; an absent conda is reported once
with install-miniconda advice, never retried), missing dependencies are
installed (`pip install -e '<repo>[test]'` under the env python), and a missing
`~/.claude/model-keys.env` is scaffolded as a commented template the operator
fills in themselves — its comments also explain how to extend the plugin to any
Anthropic-compatible provider via `~/.claude/chinamax-profiles.json`. Keys are
reported PRESENT or MISSING by name; values never appear. A healthy machine's
run mutates nothing and reports the same pure diagnosis as before. Do not
summarize — the operator acts on the specific lines.
