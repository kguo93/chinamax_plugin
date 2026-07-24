# Worker-Model Subagent Plugin

A Claude Code plugin that exposes non-Claude worker models (DeepSeek, MiMo, GLM, MiniMax, Kimi, ...) as a first-class named subagent: a thin Claude-facing bridge hands tasks to a detached, durable runtime, modeled on the OpenAI Codex plugin's orchestration.

## Language

**Bridge Agent**:
The Claude-facing named subagent — registered as `chinamax` (agent type `chinamax:chinamax`) — that accepts a task, forwards it to the Runtime, and relays errors and the final result. Exactly one Bridge serves a dispatch; it never edits files, runs the task itself, or spawns another agent.
_Avoid_: wrapper agent, proxy, "the deepseek agent" (DeepSeek is one Profile among many)

**Runtime**:
The custom agent-loop process that owns the provider API conversation, tool execution, and safety controls for a task. Speaks the provider's Anthropic-compatible Messages API (the proven `/anthropic` endpoints), not chat-completions.
_Avoid_: worker CLI, companion (reserve "companion" for the Codex plugin's runtime)

**Profile**:
A named provider configuration — base URL, model string, and API-key source — that a Job runs against (e.g. `deepseek`, `kimi`, `minimax`). One Bridge Agent serves all Profiles; a dispatch picks its Profile.
_Avoid_: provider (the company), model (one field of a profile)

**Job**:
One durable unit of dispatched work with persistent state, logs, and a lifecycle (queued, running, completed, failed, cancelled; a crashed worker's Job is reported as interrupted). Survives the Claude session that started it.
_Avoid_: task (a task is what the user asks for; a job is its tracked execution)

**Thread**:
A persistent worker-model conversation transcript belonging to a Job. Resuming carries the Thread forward into a new Job with the same provider context; a Job's follow-ups and steers land in its Thread.
_Avoid_: session, chat

**Steer**:
A message sent to a running Job — relayed by the Bridge Agent or enqueued directly. Steers land in the Job's steer queue; the Runtime drains the queue at its next loop iteration and injects each steer into the Thread as a user message.
_Avoid_: interrupt (cancellation is a different action), follow-up (a follow-up starts a new turn on a finished Thread)

**Pro**:
The only model tier the plugin offers, for every provider — each Profile pins its provider's strongest tier (deepseek-v4-pro, mimo-v2.5-pro, glm-5.2, MiniMax-M3, kimi-k3). Flash/ultraspeed tiers are never implemented as Profiles.
_Avoid_: flash, ultraspeed (never offered)
