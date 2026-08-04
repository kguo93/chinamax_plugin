# chinamax

A Claude Code plugin that exposes non-Claude worker models — **deepseek**, **mimo**,
**glm**, **minimax**, **kimi** — as a first-class named subagent. You dispatch a task
by naming a Profile; a thin Claude-facing bridge hands it to a detached, durable
runtime that owns the provider conversation, runs the tools, and reports back. The
runtime speaks each provider's Anthropic-compatible Messages API (the proven
`/anthropic` endpoints), modeled on the OpenAI Codex plugin's orchestration.

## What it is

Five terms carry the whole design (full definitions in [`CONTEXT.md`](CONTEXT.md)):

- **Bridge Agent** — the Claude-facing named subagent, registered as `chinamax`
  (agent type `chinamax:chinamax`). Each `/chinamax:task` spawns a **persistent**
  Bridge named `chinamax-<profile>-<task-slug>` that owns one Thread for the
  session's life: it forwards one dispatch to the runtime, long-polls it in
  silence, relays the outcome exactly once when the Job ends (the worker's response
  untouched, or the failure report), and then stays available to **steer**, **resume**,
  or **cancel** that Thread as you message it. Exactly one named Bridge serves a
  Thread; it never inspects the repo, edits files, spawns another agent, or does the
  task itself.
- **Runtime** — the custom agent-loop process that owns the provider API
  conversation, tool execution, and safety controls for one task.
- **Profile** — a named provider configuration (base URL, model string, API-key
  source, and fixed request tuning via `request_extras` — reasoning always on, at
  the provider's ceiling) a Job runs against. One Bridge Agent serves every
  Profile; a dispatch picks one explicitly. There is no default Profile.
- **Job** — one durable unit of dispatched work with persistent state, logs, and a
  lifecycle (`queued`, `running`, `completed`, `failed`, `cancelled`; a crashed
  worker's Job reads as `interrupted`). It is **session-scoped**: it is killed when
  the Claude session that started it ends — including `/clear` — and a Job orphaned
  by a crashed session is reaped, never resumed.
- **Thread** — the persistent worker-model transcript belonging to a Job. Resuming
  carries the Thread forward; a Job's steers and follow-ups land in it. One Bridge
  serves one Thread for its whole life.
- **Steer** — a message sent to a running Job, relayed by the Bridge Agent when you
  address it. It lands in the Job's steer queue and is injected into the Thread as a
  user message at the runtime's next loop iteration.

## Install

The repo doubles as its own single-plugin marketplace, published at
[`kguo93/chinamax_plugin`](https://github.com/kguo93/chinamax_plugin), so
installation is two supported commands:

```bash
claude plugin marketplace add kguo93/chinamax_plugin
claude plugin install chinamax@chinamax-plugin
```

`chinamax@chinamax-plugin` resolves because the marketplace is named
`chinamax-plugin` and its single plugin is named `chinamax` — never the repo's
underscored directory name.

**From a local checkout.** If you are developing the plugin itself, or want a
revision you have not pushed, point the marketplace at the root of the checkout
instead — the directory containing `.claude-plugin/marketplace.json`. The
install command is unchanged:

```bash
claude plugin marketplace add /path/to/chinamax_plugin
claude plugin install chinamax@chinamax-plugin
```

The runtime runs in a dedicated conda env named `chinamax` (Python 3.12), separate
from any other project env. Run [`/chinamax:setup`](#chinamaxsetup) — in one pass
it creates that env when missing, installs the package editable with the `[test]`
extra, scaffolds `~/.claude/model-keys.env` as a commented template when absent,
verifies the API-key entries and state-dir writability, and records the resolved
interpreter path the plugin reads first. (Miniconda itself is the one
prerequisite setup will not install — an absent conda is reported with advice.)

The equivalent manual commands, if you prefer to run them yourself:

```bash
conda create -y -n chinamax python=3.12
conda run -n chinamax pip install -e '/path/to/chinamax_plugin[test]'
```

## Configuration

### Profiles

Five Profiles ship inside the package
([`src/chinamax/data/profiles.json`](src/chinamax/data/profiles.json)), one per
provider. Each Profile's shipped model is its DEFAULT model; a dispatch may name any
model string its Profile's endpoint accepts via `model=<string>` (pro-only reversed
2026-08-03):

| Profile   | Endpoint (base URL)              | Model              | API-key variable   |
|-----------|----------------------------------|--------------------|--------------------|
| `deepseek`| `https://api.deepseek.com/anthropic`   | `deepseek-v4-pro[1m]` | `DEEPSEEK_API_KEY` |
| `mimo`    | `https://api.xiaomimimo.com/anthropic` | `mimo-v2.5-pro`       | `MIMO_API_KEY`     |
| `glm`     | `https://api.z.ai/api/anthropic`       | `glm-5.2`             | `GLM_API_KEY`      |
| `minimax` | `https://api.minimax.io/anthropic`     | `MiniMax-M3[1m]`      | `MINIMAX_API_KEY`  |
| `kimi`    | `https://api.moonshot.ai/anthropic`    | `kimi-k3`             | `KIMI_API_KEY`     |

Every shipped row also enables its provider's reasoning always-on at that
provider's ceiling, carried as a `request_extras` dict merged into every request
(deepseek/kimi max, mimo high, glm and minimax their respective "on").

Run [`/chinamax:profiles`](#chinamaxprofiles) to see the resolved rows and each
key's presence at a glance.

### Overlay: `~/.claude/chinamax-profiles.json`

An optional user overlay merges over the shipped rows field by field. It is a JSON
array of rows; each row's `name` selects the Profile:

- A row whose `name` matches a shipped Profile overrides only the fields it lists
  (e.g. point `deepseek` at a proxy by giving just `name` and `base_url`).
- A row with a new `name` adds a Profile, and must define `base_url`, `model`, and
  `api_key_env`. `max_tokens` is optional (a positive integer; default 32000).
- `request_extras` is optional — a JSON object of extra Messages-request kwargs
  (e.g. a reasoning knob) merged verbatim into every request. An overlay row
  **replaces** a Profile's dict wholesale (never a deep merge); `{}` disables
  reasoning for that Profile. Reserved keys are rejected with a named error — the
  Runtime-built request keys and the client/transport-policy kwargs (`timeout`,
  `extra_headers`, `extra_query`, `stream`), checked at the top level and inside
  an `extra_body` value — so extras can never carry credentials or override the
  Runtime on the wire.

An unknown field, a duplicate name, or a malformed file is rejected with a named
error, so a typo fails loudly rather than being silently dispatched.

### API keys: `~/.claude/model-keys.env`

Keys are read from `~/.claude/model-keys.env`, one `NAME=value` per line, using the
variable names in the table above. When the file does not exist,
[`/chinamax:setup`](#chinamaxsetup) scaffolds it as a comments-only template — one
commented `<api_key_env>=` line per resolved Profile (overlay-added Profiles
included) plus comments explaining the format and how to extend to more
Anthropic-compatible providers; an existing file is never touched. Blank lines and
`#` comments are ignored, and values are unquoted by shell rules (single-quoting a
value is fine — the same file is normally consumed by bash-sourcing it):

```
DEEPSEEK_API_KEY=...
MIMO_API_KEY=...
GLM_API_KEY=...
MINIMAX_API_KEY=...
KIMI_API_KEY=...
```

Key **values** are never printed on any stream — `profiles` and `setup` report each
key only as `PRESENT` or `MISSING` by variable name. A Profile whose key is missing
or empty fails its first dispatch with `missing API key: <NAME> is not set in
~/.claude/model-keys.env`.

### Per-dispatch flags

You steer a single dispatch with a few flags, written to the Bridge Agent via
[`/chinamax:task`](#chinamaxtask). The operator-facing spellings are on the left;
the Bridge maps them onto the CLI seam argv on the right:

| Bridge-level              | CLI seam (`chinamax task`) | Meaning                                                             |
|---------------------------|----------------------------|--------------------------------------------------------------------|
| `profile=<name>`          | `--profile <name>`         | **Required.** No default — a profile-less dispatch is refused.      |
| `model=<string>`          | `--model='<string>'`       | Optional. Any model string the Profile's endpoint accepts; omitted ⇒ the Profile's default model. Pinned to the Thread — resumes replay it. |
| `--read-only`             | `--read-only`              | Opt out of write-capable tools. Write-capable is the default.      |
| `bash_timeout=<seconds>`  | `--bash-timeout-s <seconds>` | Per-command bash timeout override (a non-numeric value is refused). |
| `poll=<seconds>`          | *(poll loop, not a task flag)* | The Bridge's long-poll bound: `status --wait --timeout-ms <seconds×1000>` (default 120 s), with the Bash timeout kept above it. A non-numeric value is refused. |

There are no `--resume`/`--fresh` routing controls. A dispatch is always a fresh
Bridge on a fresh Thread; continuing a Thread is the live Bridge's own act when you
follow up (see **Talking to a Bridge**), and a new Profile, a different model
string, or a new unrelated task is a new `/chinamax:task`.

## Commands

Four slash commands. `/chinamax:task` dispatches the Bridge Agent; the other three
(`status`, `profiles`, `setup`) are thin wrappers over the CLI seam
(`scripts/chinamax`, i.e. `python -m chinamax`) and return the seam output verbatim.
The internal seam verbs (`result`, `logs`, `cancel`, `resume`, `steer`) are no
longer exposed as commands — the Bridge drives them on your behalf.

### `/chinamax:task`
`profile=<name> [model=<string>] [--read-only] [bash_timeout=<seconds>] [poll=<seconds>] <what the worker model should do>`

Dispatch a task through the Bridge Agent. Exactly one named haiku Bridge detaches a
durable Job, then long-polls it (120 s default, or your `poll=<seconds>`) in
silence — no progress chatter, no id acknowledgment. When the Job ends, the Bridge
relays exactly one message: the worker's complete final answer (or the failure
report), untouched. The task text is delivered to the runtime on stdin, so quotes,
newlines, and leading dashes arrive byte-identical. The final response you read is
the worker's `response` exactly as the worker wrote it — as if you had dispatched
the worker model yourself. The Bridge then **stays available** for the Thread (see
**Talking to a Bridge**).

### `/chinamax:status`
`[job-id] [--wait] [--timeout-ms <ms>] [--workspace <dir>]`

With no id, list every active Job in this workspace plus the recent finished ones —
each row **bridge-first**, leading with the owning Bridge name (the Job id follows
as a secondary field). With an id (or unambiguous prefix), show that one Job with a
short progress preview. `--wait` blocks until the Job's status, phase, or log
advances, or the bound expires (`--timeout-ms`, default 240000 ms, clamped to a
900000 ms ceiling). Exit codes: `0` terminal, `2` still active.

### `/chinamax:profiles`

List every configured Profile with its endpoint, model, and API-key presence
(`PRESENT`/`MISSING` by variable name, never the value), plus the resolved
`request_extras` as compact JSON (`extras={...}`) when non-empty. Takes no
arguments.

### `/chinamax:setup`
`[--json] [--workspace <dir>]`

The environment doctor — it diagnoses **and fixes** the install in one pass; see
[Troubleshooting](#troubleshooting).

## Talking to a Bridge

Once a `/chinamax:task` is running, its Bridge is a live teammate named
`chinamax-<profile>-<task-slug>`. You interact with the Job by **addressing that
Bridge** — by its teammate name, its profile, or "the bridge"/"the worker". The
Bridge classifies each message and acts on its own Thread:

- **Steer** a running Job — send an instruction ("also update the tests", "stop
  touching module X"). It lands in the Thread at the runtime's next loop boundary,
  silently; the Bridge keeps polling and still relays exactly one terminal result.
- **Resume** after a Job ends — a follow-up ("now summarize what you changed")
  starts a new Job continuing the same Thread, inheriting the Profile and write
  posture. The Bridge relays that new Job's result when it ends.
- **Cancel** — "stop the job", "cancel", "never mind" kills the run; the Bridge
  relays the cancelled report.
- **Out of scope** — a different model is chosen AT DISPATCH via `model=<string>`;
  asking to CHANGE a live Thread's model or Profile, or to start a brand-new
  unrelated task, is refused with a pointer to dispatch a new `/chinamax:task` (a new
  Bridge). One Bridge serves one Thread and never switches its model or Profile.

Main forwards a message to a Bridge **only when you address it**; anything else is
Claude's own work. If several Bridges are live at once, name the one you mean.

## How Jobs live

**A Job is session-scoped (ADR 0004, reversed 2026-07-30).** A Job never outlives
the Claude session that started it:

- **SessionEnd** — including `/clear` — kills the ending session's still-active
  Jobs (the whole process tree) and marks their records `cancelled`.
- **SessionStart** registers the live Claude process in a session-liveness registry,
  then **reaps orphans**: any active Job whose owning session is no longer alive
  (the crash path, where SessionEnd never fired) is marked `interrupted`. It then
  injects a bounded, bridge-first digest of this workspace's running/recent Jobs (so
  a fresh session — or one after `/clear` — sees what was just terminated and any
  live Job mid-flight), and a non-blocking `Stop` hook notices still-running Jobs at
  turn's end.
- A dead session's Job ids are **never** resumed or re-attached. Continuing work is
  only ever a live Bridge Agent resuming its own Thread inside the owning session.
- **A Job never outlives its Bridge, either.** The Bridge supervises its Job through
  the same long-poll that relays it, so if the Bridge teammate dies or stops polling
  (a killed teammate, an abandoned poll loop), the session hooks reap its still-active
  Job `interrupted` with the reason `bridge terminated` (shown by `/chinamax:status`).
  That Thread is stranded — it is not resumable; dispatch a fresh `/chinamax:task` to
  continue.

Each record carries its owning `sessionId`, the owning `bridgeName`, and (for a
resume-created Job) `resumedFrom` and a `lineageRoot` — so one Bridge's whole Thread
lineage stays addressable and a resume refuses only when its own lineage is still
active, never workspace-wide.

**State root.** Every per-workspace state directory lives under one root:

- `$CLAUDE_PLUGIN_DATA/state` when `CLAUDE_PLUGIN_DATA` is set (as it usually is
  inside Claude Code),
- else `$XDG_STATE_HOME/chinamax`,
- else `~/.local/state/chinamax`.

An empty or relative value counts as unset. The session-liveness registry lives
under a sibling `sessions/` directory. The `SessionStart` hook re-exports
`CLAUDE_PLUGIN_DATA` so the hooks and the Bridge's dispatches agree on the root —
otherwise Jobs would land under one root while the digest and the reaps read
another.

**Per-workspace layout.** Within the root, each workspace gets its own directory
named `<repo-basename>-<sha256[:16]>`, keyed on the **git toplevel** of the
workspace — so dispatching from a subdirectory does not fragment one repo's state,
and a session opened in a subdirectory still finds the repo's Jobs. That directory
holds a `state.json` index, a `state.lock`, and a `jobs/` subdirectory with, per Job
(ids are `task-<base36-ms>-<random>`): `<id>.json` (the record), `<id>.log`,
`<id>.spawn.log`, `<id>.thread.jsonl` (the Thread), `<id>.result.json`, and an
`<id>.steer/` queue directory.

**Steer.** A message to a busy Bridge becomes a Steer: it is written to the Job's
`<id>.steer/` queue and drained into the Thread as a `[steer]` user message at the
runtime's next loop boundary — exactly once, even across a worker relaunch.

**Resume.** A follow-up on a finished Job dispatches a new Job continuing its
Thread, inheriting its Profile and write posture. It refuses while that Thread's own
lineage is still active, and refuses a Job whose owning session was reaped.

**Interrupted.** A crashed worker (its process *provably* gone — a reboot, an OOM
kill) reads as `interrupted`, derived read-side only; the stored status stays
`running`/`queued` and the Thread stays resumable. A dead-**session** reap, by
contrast, *writes* `interrupted` onto the record — that Thread is policy-dead and no
longer resumable. A dead-**Bridge** reap likewise *writes* `interrupted`, with the
reason `bridge terminated` — the Thread is stranded, so continuing is a fresh
`/chinamax:task`. `status` surfaces all three and points a resumable one at
continuing via its Bridge.

## Troubleshooting

### The setup doctor: `/chinamax:setup`

Run it first, and whenever a dispatch misbehaves. In one pass it diagnoses — and
**fixes** — what a first run needs:

- **conda env** — a missing `chinamax` env is created with
  `conda create -y -n chinamax python=3.12`. If conda itself is absent, that is
  reported once with install-miniconda advice, never retried.
- **dependencies** — whether `chinamax`, `anthropic`, and `pytest` import **under
  the resolved env python** (never the interpreter the doctor itself runs under, so
  a bootstrap run on a fresh machine does not grade itself); missing deps are
  installed with `pip install -e '<repo>[test]'` under that python.
- **API keys** — each Profile's key entry as `PRESENT` or `MISSING`, by variable
  name. A missing `~/.claude/model-keys.env` is scaffolded as a commented template
  (an existing file is never touched); key presence is reported but never fails
  the run — an unused Profile must not block setup.
- **state directory** — the resolved state root, this workspace's state directory,
  and whether it is writable.

A healthy machine's run mutates nothing and reports a pure diagnosis. It also
records the resolved env python at `<data root>/python-path`, which the Bridge and
the shell shims read first when resolving the interpreter.

`/chinamax:setup --json` emits a machine-readable document with the pinned fields
`ok`, `python`, `state_root`, `workspace_state_dir`, `state_writable`, `env`
(`present`/`path`), `deps`, `profiles`, and `fixes` (the fix rows the run
performed, in order, each `{action, ok, detail}`). The command exits `0` when
everything the plugin needs is in place **after** the fix pass (env present, all
three deps import, state writable), and `1` otherwise.

### Common provider errors

- **`missing API key: <NAME> is not set in ~/.claude/model-keys.env`** — the
  Profile's key variable is absent or empty. Add it to `model-keys.env`. `setup` and
  `profiles` will show that Profile as `MISSING`.
- **A present-but-revoked key still passes preflight.** `setup` and `profiles` check
  key presence by variable name only, not validity — so an invalid key reads
  `PRESENT` and then surfaces as a fast first-dispatch auth failure. If a dispatch
  fails at authentication with the key marked `PRESENT`, rotate the key value.
- **`unknown profile '<name>'`** — the Profile name is not shipped or added via the
  overlay. This fails fast, before any Job record is written; `profiles` lists the
  configured names.
- **Interpreter not resolved / wrong python.** The shims and Bridge resolve the
  interpreter in a fixed order: the path `setup` recorded, then `$CHINAMAX_PYTHON`
  (must be absolute and executable), then `~/miniconda3/envs/chinamax/bin/python`,
  then `conda run -n chinamax python`, then a bootstrap rung (system `python3` with
  `src/` on `PYTHONPATH`). Re-run `/chinamax:setup` to re-record the env python.

### Interrupted Jobs

An `interrupted` Job means its worker process is gone. If the machine rebooted or
the worker was killed mid-run while its session was still alive, the record's stored
status stays `running`/`queued` and the Thread is preserved — message the Job's
Bridge to continue it (a resume). If instead the **owning session** ended and the
Job was reaped, `interrupted` is written onto the record and that Thread is
policy-dead — it is not resumable; dispatch a fresh `/chinamax:task`. And an
`interrupted` Job whose reason is `bridge terminated` means the Job's **Bridge**
teammate died or stopped polling — the session hooks reaped the Job and that Thread
is also stranded, so dispatch a fresh `/chinamax:task`. Either way, do not wait on
an interrupted Job — it will never leave that state by itself.
