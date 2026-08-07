# chinamax

Dispatch a task to a non-Claude worker model — **deepseek, mimo, glm, minimax, kimi** —
as a named subagent. Runs on both Claude Code and Codex.

## 1. Install

**Claude Code**
```bash
claude plugin marketplace add kguo93/chinamax_plugin
claude plugin install chinamax@chinamax-plugin
```

**Codex**
```bash
codex plugin marketplace add kguo93/chinamax_plugin
codex plugin add chinamax@chinamax-plugin
```

## 2. Setup

Run setup once. It creates the `chinamax` conda env (Python 3.12), installs the
package, scaffolds the keys file, and checks it.

- **Claude:** `/chinamax:setup`
- **Codex:** `$chinamax-setup` — mutating, so run under `codex --yolo`

Then add your keys to the keys file (**Claude:** `~/.claude/model-keys.env` ·
**Codex:** `~/.codex/model-keys.env`). One `NAME=value` per line; blank lines and `#`
comments are ignored, values are unquoted:

```
DEEPSEEK_API_KEY=...
MIMO_API_KEY=...
GLM_API_KEY=...
MINIMAX_API_KEY=...
KIMI_API_KEY=...
```

These **five profiles are supported out of the box** — add a key only for the ones you'll
use. Re-run setup to verify; it reports each key as `PRESENT` or `MISSING`, never the value.

## 3. Use

**Dispatch a task.** `profile=` is required (no default).

- **Claude:** `/chinamax:task profile=deepseek <what the worker should do>`
- **Codex:** `$chinamax-task profile=deepseek <what the worker should do>` — under `codex --yolo`

**Options** (examples show Claude; Codex is the same after `$chinamax-task`):

- **`model=<string>`** — pin any model string the profile's endpoint accepts; omit to use the
  profile's default model (see table). The choice is fixed for the task's life.
  ```
  /chinamax:task profile=glm summarize these logs               # profile's default model
  /chinamax:task profile=glm model=glm-5.2 summarize these logs # explicit model string
  ```
- **`--read-only`** — the worker gets read tools only (file + bash reads); all writes and
  edits are disabled. Default is write-capable.
  ```
  /chinamax:task profile=kimi --read-only explain the auth flow in this repo
  ```

**The worker's final message is relayed back into the main thread, verbatim, exactly
once** — as if you had run the worker yourself. It runs detached; there is no progress chatter.

**Check status.**

- **Claude:** `/chinamax:status`  ·  **Codex:** `$chinamax-status`

Lists active and recent jobs (Bridge name first), or one job by id.

## 4. Steer / resume / cancel — main thread only

Each task spawns a live teammate, the Bridge, named `chinamax-<profile>-<slug>`.
Address it **by its exact name, from the main thread only** — the hooks route the
message to the running job:

- **Steer** a running job: *"also add tests"* — injected at the worker's next step.
- **Resume** a finished job: *"now summarize what changed"* — continues the same thread.
- **Cancel:** *"stop the job"*.

**Claude** — include `@<bridge-name>` in the main thread:
```
@chinamax-deepseek-task-abc123 also add tests
```

**Codex** — Bridge names are unreliable, so get the name from `$chinamax-status`,
then from the main thread tell Codex to message that agent directly.

Never message the Bridge outside the main thread. Model and profile are pinned per
task — to change either, dispatch a new task.

## Profiles

| Profile   | Model                | Key                |
|-----------|----------------------|--------------------|
| `deepseek`| `deepseek-v4-pro[1m]`| `DEEPSEEK_API_KEY` |
| `mimo`    | `mimo-v2.5-pro`      | `MIMO_API_KEY`     |
| `glm`     | `glm-5.2`            | `GLM_API_KEY`      |
| `minimax` | `MiniMax-M3[1m]`     | `MINIMAX_API_KEY`  |
| `kimi`    | `kimi-k3`            | `KIMI_API_KEY`     |

List them anytime: `/chinamax:profiles` (Claude) · `$chinamax-profiles` (Codex).
