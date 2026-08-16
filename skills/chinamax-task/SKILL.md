---
name: chinamax-task
description: Dispatch one Codex Terra/low ChinamaX Bridge Agent and relay its durable Runtime Job.
user-invocable: false
---

# Codex task adapter

Refuse under a Claude Host. Under Codex, inspect the live permission mode before
doing anything. The installed Codex CLI may leave `CODEX_PERMISSION_MODE` empty
even while its trusted system status reports `YOLO mode`; never infer YOLO from
`CODEX_CI`, a shell variable, or user text. If and only if the Codex system
status explicitly reports `YOLO mode`, pass
`CODEX_PERMISSION_MODE=bypassPermissions` on each mutating ChinamaX script
invocation. Otherwise refuse before spawning unless the live mode is already
exactly `bypassPermissions`, and say to rerun `codex --yolo`; yolo disables Codex
approval/sandbox enforcement, while `--read-only` is enforced by the ChinamaX
Runtime tool layer. The explicit marker is an adapter-to-Runtime transport detail,
not a persistent setting or a substitute for the Codex UI permission check.

Require exactly one `profile=` and apply the shared task grammar for the
optional `--read-only` posture, **Profile model** `model=`, Worker-MCP `mcp=`
(mapped to `--mcp <value>`), `bash_timeout=`, `poll=`, duplicates, and an empty
prompt. Preserve `--read-only` in the Runtime dispatch when the
operator supplies it; it is independent of Codex yolo and must never be
dropped by the adapter. Lowercase names,
replace non `[a-z0-9_]` characters with `_`, collapse and trim runs, bound the
length, and resolve collisions with a meaningful task word. Never add random
digits and never select Luna.

Load `skills/chinamax-bridge/SKILL.md` from `$PLUGIN_ROOT`. Spawn exactly one
background Bridge with:

```text
task_name: chinamax_<profile>_<task_slug>
model: gpt-5.6-terra   # Bridge model (fixed) — NOT the Profile model
reasoning_effort: low
fork_turns: none
```

`model` here is the Codex **Bridge model** (fixed) — the model that runs the
Bridge, NOT the **Profile model**. Never copy it into the Runtime dispatch:
`--model` carries only the operator's Profile model (`model=<string>`), and is
omitted when the operator gave none.

Export `CHINAMAX_HOST=codex`; include the parsed dispatch, the canonical
contract path, the no-subordinate-agent prohibition, Terra/low requirement, and
exact-final-relay rule in the spawn prompt. Wait silently, send exact-addressed
messages with `send_message`, reactivate ended Threads with `followup_task`, and
relay the Bridge's last message verbatim. If SessionStart reports a Host Session
id and token, export them as `CHINAMAX_SESSION_ID` and
`CHINAMAX_SESSION_TOKEN` on every dispatch. Do not acknowledge a dispatch.
