# Worker-Model Subagent Plugin

A dual-Host plugin that exposes non-Claude worker models (DeepSeek, MiMo, GLM, MiniMax, Kimi, ...) as a first-class named subagent: thin Claude and Codex adapters hand tasks to one detached, durable Runtime, modeled on the OpenAI Codex plugin's orchestration.

## Language

**Bridge Agent**:
The Host-facing persistent teammate — Claude agent type `chinamax:chinamax` or a Codex native Bridge, exactly one instance per Thread, named by the selected Host's safe convention — that dispatches a task to the Runtime and then serves its Thread for the Thread's whole life: it classifies every later operator message (Steer while the Job runs; resume when it has ended; cancel on explicit abandon intent), always waits for the resulting Job to end, and delivers exactly one Relay per Job it supervises. It stays silent otherwise and never edits files, runs the task itself, or spawns another agent. A Bridge that dies or abandons its poll loop forfeits its Jobs: each is reaped (interrupted) and its Thread stranded, continued only by a fresh dispatch.
_Avoid_: wrapper agent, proxy, "the deepseek agent" (DeepSeek is one Profile among many)

**Inbound Bridge message**:
Externally authored text accepted for model presentation in a Bridge Thread,
including initial spawn, idle follow-up, running queue/steer delivery, and any
future supported direct Bridge route. Tool results, hook context, status,
Bridge output, and compaction without new external text are not inbound
messages. A Codex Bridge is bound by its persistent custom-agent developer
contract before the next model request; native queue/interrupt semantics remain
unchanged.
_Avoid_: turn (queued messages may share a turn id), tool result, hook context

**Relay**:
The Bridge Agent's single terminal message delivering a Job's outcome to the operator: the worker's final response untouched when the Job completed, or the failure report otherwise. Exactly one per Job, sent only when the Job ends — never a progress update, never an acknowledgment. The operator reads the worker's response as if they had dispatched the worker model themselves.
_Avoid_: progress message, notification, status update (none of these are ever sent)

**Follow stream**:
The live tail of a running Job's progress log that the Bridge Agent's poll prints in its own terminal pane while waiting. Pane-only visibility the operator opts into by expanding the Bridge Agent: it is never a Relay, never sent as a message, and the Runtime tracks the streamed position so the tail is continuous across poll calls.
_Avoid_: progress relay, progress message (a Relay is terminal-only; the stream is not a message)

**Runtime**:
The custom agent-loop process that owns the provider API conversation, tool execution, and safety controls for a task. Speaks the provider's Anthropic-compatible Messages API (the proven `/anthropic` endpoints), not chat-completions.
_Avoid_: worker CLI, companion (reserve "companion" for the Codex plugin's runtime)

**Profile**:
A named provider configuration — base URL, default model string, API-key source, and fixed request tuning (reasoning always on, at the provider's ceiling) — that a Job runs against (e.g. `deepseek`, `kimi`, `minimax`). One Bridge Agent serves all Profiles; a dispatch picks its Profile and may name a model string of its own.
_Avoid_: provider (the company), model (one field of a profile)

**Bridge model**:
The model that runs the Bridge Agent itself — fixed per Host (Claude: Sonnet; Codex: `gpt-5.6-terra`), never configurable by the task prompt. Distinct from the Profile model and never sent to the worker or provider endpoint.
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
The model string a Profile resolves to (shipped row, overlay-adjustable) — what every dispatch that names no model runs against (deepseek-v4-pro[1m], mimo-v2.5-pro, glm-5.2, MiniMax-M3[1m], kimi-k3, qwen3.8-max). A dispatch may name any other model string; the provider endpoint is the only judge of validity. Tier names (pro, flash, ultraspeed) are marketing labels, not plugin concepts.
_Avoid_: pro/flash/ultraspeed as selection concepts (marketing labels), tier

**Pinned model**:
A model string a dispatch named explicitly, fixed to its Thread for life: every resume replays it and the Bridge Agent never changes it. A dispatch that names none pins nothing — its Thread follows the Profile's current default model.
_Avoid_: model override (transient-sounding; the pin lasts the Thread's life)

**Host**:
The selected plugin host — `claude` or `codex` — resolved once at a process
boundary. It determines native adapter behavior and filesystem roots; it is not
a provider Profile.

**Platform**:
The operating-system family on which a Host and the ChinamaX Runtime execute:
Linux, macOS, or Windows. Platform is orthogonal to Host. It selects native path,
process, lock, permission, and launcher mechanisms but never changes Job, Thread,
provider, confinement, or result semantics.

**Host Session**:
The host-owned lifecycle identity recorded on a Job. Claude uses the existing
session registry semantics; Codex additionally uses a token so a detached
SessionEnd reaper cannot delete a newer session's registry or Job ownership.

**Host adapter**:
The thin native surface for a Host: manifests, routing/message tools, paths,
lifecycle hooks, and setup. It loads the shared Runtime and canonical Bridge
contract rather than maintaining a second implementation.

**Prerequisite**:
An external Platform tool the Runtime environment cannot be built without —
bash, Git for Windows' git/bash/cygpath, Miniconda's conda — detected per
Platform early in setup, before any Prerequisite is installed or any env/deps/key
fixer runs (the only Phase-A side effect is the doctor's idempotent state-probe
and interpreter record). Distinct from the Python dependencies installed inside
the env. A missing Prerequisite pauses setup for operator approval; it is never
installed silently.
_Avoid_: dependency (reserved for the Python packages in the env), requirement

**Rectification command**:
The exact runnable command line(s) setup emits for one missing Prerequisite on
the current Platform — the single source of truth for how that Prerequisite
gets installed. The Host agent executes them verbatim only after the operator
approves; the Runtime never runs them itself. A command needing interactive
privilege is marked for the operator to run in their own terminal.
_Avoid_: calling one a "fix" in prose (that word is the doctor's own env/deps/key fixers; the serialized report key is still `prerequisite_fixes`), installer script

**Policy hook**:
An operator-authored hook sourced from the selected Host's settings surfaces
(Claude: managed/user/project/local settings files; Codex: the CLI's own hook
configuration) that the Runtime enforces at a worker's tool and stop seams —
the worker obeys the same deny/allow/context decisions the Host harness would
deliver. Plugin-registered hooks are never Policy hooks: workers do not load
plugins, and plugin hooks are host-session machinery.
_Avoid_: plugin hook (excluded by definition), "custom hook" (ambiguous)

**Memory file**:
A Host memory document (CLAUDE.md and its local/import companions on Claude;
AGENTS.md on Codex) in the discovery chain for a workspace — the Host-global
file, the workspace's ancestor chain, and lazily the subdirectories a worker
touches. The operator's Claude memory store (MEMORY.md and its directory) is
never a Memory file.
_Avoid_: "CLAUDE.md reads" (the worker doesn't read them; they're injected)

**Memory injection**:
The delimited block that carries Memory-file content into a worker Thread:
prepended to the first user turn when a fresh Job starts, or delivered
alongside a tool result when a touched subdirectory's Memory file loads
lazily. Resumed Jobs never re-inject; the Thread's existing injection stands.
_Avoid_: system-prompt injection (it is never placed there), reminder

**Worker MCP**:
The per-Job connections to the Host's configured MCP servers whose tools are
advertised to the worker alongside the native tool roster and governed by the
same Policy hooks. The per-Host `mcp` Policy toggle turns the whole capability
on or off; when on, the RESOLVED server-name selection is pinned at dispatch and
rides with the Thread across resumes.
_Avoid_: conflating with the Host harness's own MCP connections (workers
connect independently); a per-dispatch `mcp=` selector (removed in 0.7.0 — the
toggle is the sole control)

**Policy toggles**:
The three per-Host booleans — `memory`, `hooks`, `mcp` — each governing its
ENTIRE ADR 0016 capability (Memory injection, Policy hooks, Worker MCP). They
live in a per-Host `settings.json` under the state root, default OFF (absent
file or key ⇒ off), and are resolved once and PINNED at dispatch so a Thread
keeps the policy it was dispatched with. A malformed file fails a new dispatch.
_Avoid_: conflating the toggle file with Claude's own `~/.claude/settings.json`
(same basename, different file and schema); "flag" (they are not CLI flags)

**Scratch root**:
The Platform's temporary-file directory, which a Job's file tools may touch in
addition to the workspace — an escape hatch for worker scratch files. Resolved
by the Platform's standard temp convention (`TMPDIR`, `%TEMP%`), so it is not
literally `/tmp` everywhere; the whole directory is permitted, and the worker
is told its resolved path. Read-only Jobs may only read it; bash stays pinned
to the workspace.
_Avoid_: "/tmp" (a Platform-specific value, wrong on macOS), temp workspace,
second workspace
