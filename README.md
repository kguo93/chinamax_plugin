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
  (agent type `chinamax:chinamax`). It forwards one dispatch to the runtime, then
  long-polls it — relaying errors and the terminal result — and forwards mid-run
  messages as steers. Exactly one named Bridge serves a dispatch; it never inspects
  the repo, edits files, spawns another agent, or does the task itself.
- **Runtime** — the custom agent-loop process that owns the provider API
  conversation, tool execution, and safety controls for one task.
- **Profile** — a named provider configuration (base URL, model string, API-key
  source) a Job runs against. One Bridge Agent serves every Profile; a dispatch
  picks one explicitly. There is no default Profile.
- **Job** — one durable unit of dispatched work with persistent state, logs, and a
  lifecycle (`queued`, `running`, `completed`, `failed`, `cancelled`; a crashed
  worker's Job reads as `interrupted`). It survives the Claude session that started
  it.
- **Thread** — the persistent worker-model transcript belonging to a Job. Resuming
  carries the Thread forward; a Job's steers and follow-ups land in it.
- **Steer** — a message sent to a running Job, relayed by the Bridge Agent or
  enqueued directly with `/chinamax:steer`. It lands in the Job's steer queue and is
  injected into the Thread as a user message at the runtime's next loop iteration.

## Install

The repo doubles as its own single-plugin marketplace, so installation is two
supported commands. Point the marketplace at the root of this checkout (the
directory containing `.claude-plugin/marketplace.json`):

```bash
claude plugin marketplace add /path/to/deepseek_plugin
claude plugin install chinamax@deepseek-plugin
```

`chinamax@deepseek-plugin` resolves because the marketplace is named
`deepseek-plugin` and its single plugin is named `chinamax` — never the repo's
underscored directory name.

The runtime runs in a dedicated conda env named `chinamax` (Python 3.12), separate
from any other project env. Create it and install the package editable, with the
`[test]` extra so the setup doctor's own dependency check is satisfied:

```bash
conda create -y -n chinamax python=3.12
conda run -n chinamax pip install -e '/path/to/deepseek_plugin[test]'
```

Then run [`/chinamax:setup`](#chinamaxsetup) — it verifies the env, the
dependencies, the API-key entries, and state-dir writability in one pass, and
records the resolved interpreter path the plugin reads first.

## Configuration

### Profiles

Five Profiles ship inside the package
([`src/chinamax/data/profiles.json`](src/chinamax/data/profiles.json)) — pro tiers
only, one per provider:

| Profile   | Endpoint (base URL)              | Model              | API-key variable   |
|-----------|----------------------------------|--------------------|--------------------|
| `deepseek`| `https://api.deepseek.com/anthropic`   | `deepseek-v4-pro[1m]` | `DEEPSEEK_API_KEY` |
| `mimo`    | `https://api.xiaomimimo.com/anthropic` | `mimo-v2.5-pro`       | `MIMO_API_KEY`     |
| `glm`     | `https://api.z.ai/api/anthropic`       | `glm-5.2`             | `GLM_API_KEY`      |
| `minimax` | `https://api.minimax.io/anthropic`     | `MiniMax-M3[1m]`      | `MINIMAX_API_KEY`  |
| `kimi`    | `https://api.moonshot.ai/anthropic`    | `kimi-k3`             | `KIMI_API_KEY`     |

Run [`/chinamax:profiles`](#chinamaxprofiles) to see the resolved rows and each
key's presence at a glance.

### Overlay: `~/.claude/chinamax-profiles.json`

An optional user overlay merges over the shipped rows field by field. It is a JSON
array of rows; each row's `name` selects the Profile:

- A row whose `name` matches a shipped Profile overrides only the fields it lists
  (e.g. point `deepseek` at a proxy by giving just `name` and `base_url`).
- A row with a new `name` adds a Profile, and must define `base_url`, `model`, and
  `api_key_env`. `max_tokens` is optional (a positive integer; default 32000).

An unknown field, a duplicate name, or a malformed file is rejected with a named
error, so a typo fails loudly rather than being silently dispatched.

### API keys: `~/.claude/model-keys.env`

Keys are read from `~/.claude/model-keys.env`, one `NAME=value` per line, using the
variable names in the table above. Blank lines and `#` comments are ignored, and
values are unquoted by shell rules (single-quoting a value is fine — the same file
is normally consumed by bash-sourcing it):

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

You steer a single dispatch with a few flags. Written to the Bridge Agent (via
[`/chinamax:task`](#chinamaxtask) or by addressing the agent), the operator-facing
spellings are on the left; the Bridge maps them onto the CLI seam argv on the right:

| Bridge-level              | CLI seam (`chinamax task`) | Meaning                                                             |
|---------------------------|----------------------------|--------------------------------------------------------------------|
| `profile=<name>`          | `--profile <name>`         | **Required** on a fresh dispatch. No default — a profile-less dispatch is refused. |
| `--read-only`             | `--read-only`              | Opt out of write-capable tools. Write-capable is the default.      |
| `bash_timeout=<seconds>`  | `--bash-timeout-s <seconds>` | Per-command bash timeout override (a non-numeric value is refused). |
| `poll=<seconds>`          | *(poll loop, not a task flag)* | The Bridge's long-poll bound: `status --wait --timeout-ms <seconds×1000>` (default 900 s), with the Bash timeout kept above it. A non-numeric value is refused. |
| `--resume` / `--fresh`    | *(routing, not passed through)* | Bridge-level routing: `--fresh` (or a first dispatch) routes to `task`; `--resume` (or a natural-language follow-up) routes to `resume`. |

`--resume` and `--fresh` are Bridge-level **routing controls**, never seam flags —
the Bridge picks the `task` or `resume` verb and never forwards them as arguments. A
`resume` continues a Thread whose Profile is already fixed, so it takes no
`profile=`.

## Commands

Nine slash commands. `/chinamax:task` dispatches the Bridge Agent; the other
eight are thin wrappers over the same CLI seam (`scripts/chinamax`, i.e. `python
-m chinamax`) and return the seam output verbatim.

### `/chinamax:task`
`profile=<name> [--read-only] [--resume|--fresh] [bash_timeout=<seconds>] [poll=<seconds>] <what the worker model should do>`

Dispatch a task through the Bridge Agent. Exactly one named haiku Bridge detaches a
durable Job, relays its id immediately, then long-polls it (900 s default, or your
`poll=<seconds>`) — relaying errors and the terminal result only, with no progress
chatter — and forwards any mid-run message you send as a Steer. The task text is
delivered to the runtime on stdin, so quotes, newlines, and leading dashes arrive
byte-identical. The final response is the worker's result with its `report_result`
envelope stripped and the worker's own prose left untouched.

### `/chinamax:status`
`[job-id] [--wait] [--timeout-ms <ms>] [--workspace <dir>]`

With no id, list every active Job in this workspace plus the recent finished ones.
With an id (or unambiguous prefix), show that one Job with a short progress preview.
`--wait` blocks until the Job's status, phase, or log advances, or the bound expires
(`--timeout-ms`, default 240000 ms, clamped to a 900000 ms ceiling so the Bridge's
long-poll is honored rather than capped). Exit codes: `0` terminal, `2` still active.

### `/chinamax:result`
`[job-id] [--json] [--workspace <dir>]`

Print a finished Job's stored result — the worker's `report_result` payload
verbatim. With no id, the latest `completed` Job. A `failed`, `cancelled`, or
`interrupted` Job prints its status and error instead of a payload; an active Job is
refused (exit `2`). `--json` emits the stored result artifact as JSON.

### `/chinamax:cancel`
`[job-id] [--workspace <dir>]`

Stop a running Job — kill its whole process tree and mark the record `cancelled`.
With no id, the single active Job; several active Jobs are listed rather than
guessed. A Job that completed during the kill keeps its result. If a targeted
process survives `SIGKILL`, cancel reports it and does **not** write `cancelled`.

### `/chinamax:resume`
`[job-id] <follow-up prompt>`

Continue a finished Job's Thread with a follow-up — a new Job that inherits the
source's Profile and write posture. Name the source Job id first (else the most
recent non-active Job with a Thread is used). It refuses while any Job in the
workspace is still active. Takes no `profile=`.

### `/chinamax:steer`
`[job-id] <steer message>`

Enqueue a mid-run message onto a running Job — it lands in the Thread as a user
message at the runtime's next loop boundary. With no id the single active Job is
targeted; several active Jobs are listed rather than guessed. A finished or
interrupted Job is refused with a pointer to `resume`. This is the same Steer the
Bridge forwards, enqueued directly from the main context with none of the long
poll's latency.

### `/chinamax:logs`
`<job-id> [--tail <n>] [--workspace <dir>]`

Print a Job's timestamped progress log (`--tail <n>` for the last N lines). When the
progress log is empty — a worker that died on import before writing anything — it
falls back to the spawn log.

### `/chinamax:profiles`

List every configured Profile with its endpoint, model, and API-key presence
(`PRESENT`/`MISSING` by variable name, never the value). Takes no arguments.

### `/chinamax:setup`
`[--json] [--workspace <dir>]`

The environment doctor — see [Troubleshooting](#troubleshooting).

## How Jobs live

**A Job outlives the Claude session that started it (ADR 0004).** There is no
SessionEnd hook and nothing reaps state at a session boundary; a session ending
never touches a running worker. Instead, a `SessionStart` hook injects a bounded
digest of this workspace's running/recent Jobs (so a fresh session — or one after
`/clear` — inherits awareness of a long Job mid-flight), and a non-blocking `Stop`
hook notices any still-running Jobs at turn's end. The originating session id is kept
only as provenance; no lifecycle behavior keys off it.

**State root.** Every per-workspace state directory lives under one root:

- `$CLAUDE_PLUGIN_DATA/state` when `CLAUDE_PLUGIN_DATA` is set (as it usually is
  inside Claude Code),
- else `$XDG_STATE_HOME/chinamax`,
- else `~/.local/state/chinamax`.

An empty or relative value counts as unset. The `SessionStart` hook re-exports
`CLAUDE_PLUGIN_DATA` so the hooks and the Bridge's dispatches agree on the root —
otherwise Jobs would land under one root while the digest read another.

**Per-workspace layout.** Within the root, each workspace gets its own directory
named `<repo-basename>-<sha256[:16]>`, keyed on the **git toplevel** of the
workspace — so dispatching from a subdirectory does not fragment one repo's state,
and a session opened in a subdirectory still finds the repo's Jobs. That directory
holds a `state.json` index, a `state.lock`, and a `jobs/` subdirectory with, per Job
(ids are `task-<base36-ms>-<random>`): `<id>.json` (the record), `<id>.log`,
`<id>.spawn.log`, `<id>.thread.jsonl` (the Thread), `<id>.result.json`, and an
`<id>.steer/` queue directory.

**Steer.** A message to the busy Bridge — or a direct `/chinamax:steer` — becomes a
Steer: it is written to the Job's `<id>.steer/` queue and drained into the Thread as
a `[steer]` user message at the runtime's next loop boundary — exactly once, even
across a worker relaunch. A steer to a finished Job is refused and re-routed to
`resume`.

**Resume.** `resume` dispatches a new Job that continues a finished Job's Thread,
inheriting its Profile and write posture. It refuses while any Job in the workspace
is still active.

**Interrupted.** When a worker is *provably* gone (its process no longer exists —
a reboot, an OOM kill), the Job reads as `interrupted`. This is derived read-side
only; it is **never written onto the record**, so the stored status stays
`running`/`queued` and the Thread stays resumable. `status` and `result` surface
`interrupted` and point you at `resume <id>` to continue the Thread.

## Troubleshooting

### The setup doctor: `/chinamax:setup`

Run it first, and whenever a dispatch misbehaves. In one pass it reports:

- **conda env** — whether the `chinamax` env exists; when it does not, it prints the
  exact `conda create` / `pip install -e` commands to create it.
- **dependencies** — whether `chinamax`, `anthropic`, and `pytest` import **under
  the resolved env python** (never the interpreter the doctor itself runs under, so
  a bootstrap run on a fresh machine does not grade itself).
- **API keys** — each Profile's key entry as `PRESENT` or `MISSING`, by variable
  name. Key presence is reported but never fails the run — an unused Profile must
  not block setup.
- **state directory** — the resolved state root, this workspace's state directory,
  and whether it is writable.

It also records the resolved env python at `<data root>/python-path`, which the
Bridge and the shell shims read first when resolving the interpreter.

`/chinamax:setup --json` emits a machine-readable document with the pinned fields
`ok`, `python`, `state_root`, `workspace_state_dir`, `state_writable`, `env`
(`present`/`path`), `deps`, and `profiles`. The command exits `0` when everything
the plugin needs is in place (env present, all three deps import, state writable),
and `1` otherwise.

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

An `interrupted` Job means its worker process is gone and the Job will not progress
on its own — typically the machine rebooted or the worker was killed mid-run. It is
not a completion. The Thread is preserved, so `/chinamax:resume <id> <prompt>`
continues exactly where it left off. Do not wait on an interrupted Job — it will
never leave that state by itself.
