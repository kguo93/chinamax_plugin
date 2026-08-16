---
description: Dispatch a task to a non-Claude worker model through the ChinamaX Bridge Agent.
argument-hint: "profile=<name> [model=<string>] [mcp=<none|comma-list>] [--read-only] [bash_timeout=<seconds>] [poll=<seconds>] <what the worker model should do>"
allowed-tools: Agent
---

Make exactly one INLINE background named `Agent` call with
`subagent_type: "chinamax:chinamax"`, explicit `model: "haiku"`, and a
`name` of `chinamax-<profile>-<task-slug>`. Export `CHINAMAX_HOST=claude` and
load the canonical `skills/chinamax-bridge/SKILL.md` from
`$CLAUDE_PLUGIN_ROOT` in the spawn prompt before any seam call.

The explicit `model: "haiku"` in the Agent call above is the Claude **Bridge
model** — the model that runs the Bridge itself. It is NOT the **Profile model**
and must never be copied into the Runtime dispatch. The optional `model=<string>`
below is the **Profile model**, the worker model string.

The shared CLI mapping is `--profile <name>`, `--read-only`,
`--bash-timeout-s <seconds>`, `--model='<string>'`, `--mcp <value>`, and
`--bridge-name <exact-name>`.

The Bridge must validate the existing `profile=`, optional **Profile model**
`model=<string>` mapped as `--model='<string>'`, optional Worker-MCP
`mcp=<none|comma-list>` mapped as `--mcp <value>`, `bash_timeout=`, `poll=`,
duplicate, and empty-prompt grammar; map the operator's Profile model
`model=<string>` to `--model='<string>'`, and omit `--model` when no `model=`
was given; map `mcp=` to `--mcp <value>` and omit `--mcp` when no `mcp=` was given; **Profile model** values reject spaces or quote characters. Never put
the Bridge model into `--model` or send it to the worker. Pass the prompt through
a quoted
stdin heredoc; dispatch one durable Job with `--bridge-name`; poll with
`--timeout-ms 120000` and a 180000 ms Bash bound by exit code;
stay silent while active; and send exactly one terminal message to `main` after
stripping only the Runtime result header. The main response is that message
verbatim. The Bridge never performs the task or spawns another agent. It remains
addressable for exact-name steer, resume, and cancel messages. Profile-only and
generic “bridge/worker” references are not routing addresses.

The canonical contract is authoritative for “never do the task yourself”,
“classify each message from main”, exactly one `SendMessage(to='main')`, no
progress messages, and the STEER choice when unsure between cancel and steer.
The Bridge must send exactly one SendMessage(to='main') at terminal.

Raw dispatch request:

$ARGUMENTS
