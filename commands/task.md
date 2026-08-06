---
description: Dispatch a task to a non-Claude worker model through the ChinamaX Bridge Agent.
argument-hint: "profile=<name> [model=<string>] [--read-only] [bash_timeout=<seconds>] [poll=<seconds>] <what the worker model should do>"
allowed-tools: Agent
---

Make exactly one INLINE background named `Agent` call with
`subagent_type: "chinamax:chinamax"`, explicit `model: "haiku"`, and a
`name` of `chinamax-<profile>-<task-slug>`. Export `CHINAMAX_HOST=claude` and
load the canonical `skills/chinamax-bridge/SKILL.md` from
`$CLAUDE_PLUGIN_ROOT` in the spawn prompt before any seam call.

The shared CLI mapping is `--profile <name>`, `--read-only`,
`--bash-timeout-s <seconds>`, `--model='<string>'`, and
`--bridge-name <exact-name>`.

The Bridge must validate the existing `profile=`, `model=<string>` mapped as
`--model='<string>'`, `bash_timeout=`, `poll=`, duplicate, and empty-prompt grammar;
model values reject spaces or quote characters; pass the prompt through a quoted
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
It refuses a different model string or a new unrelated task, and the Bridge
must send exactly one SendMessage(to='main') at terminal.

Raw dispatch request:

$ARGUMENTS
