# Worker-Model Subagent Plugin

A dual-Host plugin that exposes non-Claude worker models (DeepSeek, MiMo, GLM, MiniMax, Kimi, ...) as a first-class named subagent: thin Claude and Codex adapters hand tasks to one detached, durable Runtime, modeled on the OpenAI Codex plugin's orchestration.

## Language

**Bridge Agent**:
The Host-facing persistent teammate — Claude agent type `chinamax:chinamax` or a Codex native Bridge, exactly one instance per Thread, named by the selected Host's safe convention — that dispatches a task to the Runtime and then serves its Thread for the Thread's whole life: it classifies every later operator message (Steer while the Job runs; resume when it has ended; cancel on explicit abandon intent), always waits for the resulting Job to end, and delivers exactly one Relay per Job it supervises. It stays silent otherwise and never edits files, runs the task itself, or spawns another agent. A Bridge that dies or abandons its poll loop forfeits its Jobs: each is reaped (interrupted) and its Thread stranded, continued only by a fresh dispatch.
_Avoid_: wrapper agent, proxy, "the deepseek agent" (DeepSeek is one Profile among many)

**Relay**:
The Bridge Agent's single terminal message delivering a Job's outcome to the operator: the worker's final response untouched when the Job completed, or the failure report otherwise. Exactly one per Job, sent only when the Job ends — never a progress update, never an acknowledgment. The operator reads the worker's response as if they had dispatched the worker model themselves.
_Avoid_: progress message, notification, status update (none of these are ever sent)

**Runtime**:
The custom agent-loop process that owns the provider API conversation, tool execution, and safety controls for a task. Speaks the provider's Anthropic-compatible Messages API (the proven `/anthropic` endpoints), not chat-completions.
_Avoid_: worker CLI, companion (reserve "companion" for the Codex plugin's runtime)

**Profile**:
A named provider configuration — base URL, default model string, API-key source, and fixed request tuning (reasoning always on, at the provider's ceiling) — that a Job runs against (e.g. `deepseek`, `kimi`, `minimax`). One Bridge Agent serves all Profiles; a dispatch picks its Profile and may name a model string of its own.
_Avoid_: provider (the company), model (one field of a profile)

**Bridge model**:
The model that runs the Bridge Agent itself — fixed per Host (Claude: Haiku; Codex: `gpt-5.6-terra`), never configurable by the task prompt. Distinct from the Profile model and never sent to the worker or provider endpoint.
_Avoid_: conflating with the Profile's `model`, "the worker model"

**Profile model**:
The worker model string dispatched to the Runtime — the operator's optional `model=<string>` (→ `--model='<string>'`), or the Profile's default when omitted; pinned to the Thread.
_Avoid_: the Bridge model

**Job**:
One durable unit of dispatched work with persistent state, logs, and a lifecycle (queued, running, completed, failed, cancelled; a crashed worker's Job is reported as interrupted). Owned by the Host Session that started it AND supervised by its Bridge, and never outlives either when the Host lifecycle is delivered: Claude reaps synchronously; Codex uses a token-safe detached reaper and may leave an orphan process after abrupt close. A Job whose Bridge dies or stops serving it is likewise reaped (interrupted) and its Thread stranded.
_Avoid_: task (a task is what the user asks for; a job is its tracked execution)

**Thread**:
A persistent worker-model conversation transcript belonging to a Job, served by exactly one Bridge Agent for its whole life. Resuming carries the Thread forward into a new Job with the same provider context; a Job's follow-ups and steers land in its Thread.
_Avoid_: session, chat

**Steer**:
A message sent to a running Job — an operator message the Bridge Agent classified and forwarded. Steers land in the Job's steer queue; the Runtime drains the queue at its next loop iteration and injects each steer into the Thread as a user message.
_Avoid_: interrupt (cancellation is a different action), follow-up (a follow-up starts a new turn on a finished Thread)

**Default model**:
The model string a Profile resolves to (shipped row, overlay-adjustable) — what every dispatch that names no model runs against (deepseek-v4-pro[1m], mimo-v2.5-pro, glm-5.2, MiniMax-M3[1m], kimi-k3). A dispatch may name any other model string; the provider endpoint is the only judge of validity. Tier names (pro, flash, ultraspeed) are marketing labels, not plugin concepts.
_Avoid_: pro/flash/ultraspeed as selection concepts (marketing labels), tier

**Pinned model**:
A model string a dispatch named explicitly, fixed to its Thread for life: every resume replays it and the Bridge Agent never changes it. A dispatch that names none pins nothing — its Thread follows the Profile's current default model.
_Avoid_: model override (transient-sounding; the pin lasts the Thread's life)

**Host**:
The selected plugin host — `claude` or `codex` — resolved once at a process
boundary. It determines native adapter behavior and filesystem roots; it is not
a provider Profile.

**Host Session**:
The host-owned lifecycle identity recorded on a Job. Claude uses the existing
session registry semantics; Codex additionally uses a token so a detached
SessionEnd reaper cannot delete a newer session's registry or Job ownership.

**Host adapter**:
The thin native surface for a Host: manifests, routing/message tools, paths,
lifecycle hooks, and setup. It loads the shared Runtime and canonical Bridge
contract rather than maintaining a second implementation.
