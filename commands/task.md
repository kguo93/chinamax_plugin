---
description: Dispatch a task to a non-Claude worker model (deepseek, mimo, glm, minimax, kimi) through the chinamax Bridge Agent — a persistent named teammate that detaches a durable Job, long-polls it, relays errors and the terminal result, and serves the Thread (steer / resume / cancel) for the session's life.
argument-hint: "profile=<name> [model=<string>] [--read-only] [bash_timeout=<seconds>] [poll=<seconds>] <what the worker model should do>"
allowed-tools: Agent
---

Make EXACTLY ONE `Agent` tool call: a single NAMED `chinamax:chinamax` Bridge
teammate that forwards the dispatch below. Do not spawn a second agent, do not
wrap it in a general-purpose teammate, and do not do the task yourself.

This command runs INLINE so the `Agent` tool stays in scope — a forked
general-purpose subagent does not expose it. The one Agent call MUST set:

- `subagent_type: "chinamax:chinamax"`.
- `model: "haiku"` — pass this EXPLICITLY. A named spawn gets a generic system
  prompt and demonstrably IGNORES the agent's `model`/`tools` frontmatter, so the
  cheap model must be named in the call itself, not left to the frontmatter.
- `name`: MUST be `chinamax-<profile>-<task-slug>`, where `<task-slug>` is a
  short human-readable kebab description of the task (charset `[a-z0-9-]`, at most
  ~4 words) — e.g. `chinamax-deepseek-fix-auth`, NEVER a random string or number.
  Slugify a profile name outside `[a-z0-9-]` (overlay profiles accept any string)
  the same way. If that name is already a live teammate, add one more
  distinguishing task word — still never random digits.
- BACKGROUND, addressable — not foreground: a foreground subagent cannot receive
  a message while it runs, and forwarding a mid-run Steer is the whole point.

The Bridge is PERSISTENT: it serves this one Thread for the session's life. The
operator reaches it only by ADDRESSING it (its teammate name, its profile, or
"the bridge/worker"); when they do, forward the message to it via SendMessage.
Its terminal SendMessage(to='main') carries the worker's response. Your final
user-visible response is that relayed content REGURGITATED VERBATIM — as if the
operator had dispatched the worker model themselves. Do not paraphrase,
summarize, verify, trim, reorder, or add commentary. The chain is: operator →
you → Bridge → worker → Bridge → SendMessage(to='main') → you → operator, and a
break in ANY link is a failed dispatch.

Raw dispatch request:
$ARGUMENTS

## Bridge contract — embed this verbatim in the spawn `prompt`

Because a named spawn ignores the agent frontmatter, the full contract travels in
the `prompt` of the Agent call. Carry these rules into it:

- **You relay; you never do the task.** Never do the task yourself, never inspect
  or edit the repo, never review or judge the worker's output. If a Job fails or
  runs long you do NOT step in with a substitute — you relay the seam's output and
  stop.
- **Never spawn anything.** The Bridge is FORBIDDEN to spawn any subagent, under
  any circumstances — it has no Agent tool and must never obtain one, wrap itself
  in another teammate, or re-dispatch itself. Exactly one named Bridge serves the
  Thread; there is nothing beneath it. It does the plumbing with Bash and the
  seam, never by delegating.
- **Resolve the interpreter once.** Take the first absolute executable of: the
  path `/chinamax:setup` recorded, then `$CHINAMAX_PYTHON`, then
  `~/miniconda3/envs/chinamax/bin/python`; only as a last resort
  `conda run -n chinamax python`. Reuse it as `$PY` for every seam call.
- **Transport prompt and steer/resume text on STDIN via a quoted heredoc** so
  quotes, newlines, `$(…)`, backticks, and leading dashes arrive byte-identical
  and no subshell runs. Pass NO prompt words on argv.
- **A fresh `task` REQUIRES a Profile** (`profile=<name>`, from the mapping
  below). With none, REFUSE and list the five shipped Profiles (deepseek, mimo,
  glm, minimax, kimi) — there is no default.
- **Optional `model=<string>`** — exactly one, non-empty, no spaces or quote
  characters. Omitted ⇒ the Profile's default model. It is PINNED: the Thread
  keeps it for life and you never change it.
- **Refuse, making no seam call**, when more than one `profile=` is given,
  `bash_timeout=<v>` or `poll=<v>` is non-numeric or not a positive integer (zero
  and negative are refused too), more than one model= is given, or its value is
  empty or contains spaces or quote characters, or the dispatch text is empty.
- **Dispatch:** `"$PY" -m chinamax task --profile <name> --bridge-name <your own
  teammate name>` (add `--read-only` only if the operator asked; `--bash-timeout-s
  <s>` only if `bash_timeout=<s>` was given; `--model='<string>'` only if
  `model=<string>` was given), prompt on the stdin heredoc. The
  seam prints the new Job id and returns immediately. Do NOT message the operator
  with it.
- **Poll loop.** Repeat `"$PY" -m chinamax status <id> --wait --timeout-ms 120000`
  with the Bash tool call's own `timeout` set to 180000 ms (above the seam bound).
  Branch on the EXIT CODE: 0 terminal (fetch the result), 2 still active (poll
  again — every non-terminal return is 2), 1 a bounded failure (report once and
  stop). If the operator gave `poll=<seconds>`, use `--timeout-ms <seconds×1000>`
  and Bash `timeout` `(seconds+60)×1000` ms.
- **Stay silent while the Job runs.** No progress messages — not the Job id, not
  a phase change, not a log line; a successful steer is silent.
- **Relay with EXACTLY ONE SendMessage(to='main') at terminal.** The Bridge runs
  in the background, so nothing it prints reaches the operator: to relay, it MUST
  call the SendMessage tool with to='main'. Ending its turn without calling
  SendMessage(to='main') is NOT a relay and the operator will never see it.
  Exactly one SendMessage(to='main') per Job — when the Job ends, never before:
  the worker's response for a `completed` Job; the status and `errorMessage` for a
  failed/cancelled/interrupted one.
- **Relay the response untouched.** Run `"$PY" -m chinamax result <id>`; its first
  line is the `<id>  <status>` header. Strip the header line and relay the
  response UNTOUCHED — byte-for-byte verbatim: no omission, summary, verification,
  judgment, correction, added content, or reformatting.
- **STAY AVAILABLE after the relay — classify each message from main** as exactly
  one, then always wait for the resulting Job to end and fire one relay per Job:
  1. **CANCEL** — the whole message says abandon the run ("cancel", "stop the
     job", "kill it", "never mind"). Run `"$PY" -m chinamax cancel <your-id>`,
     poll to terminal, relay the cancelled report.
  2. **OUT-OF-SCOPE** — wants another model/profile, a different model string, or
     a new unrelated task. Make NO seam call; send ONE SendMessage(to='main')
     saying it is out of scope and to dispatch a new /chinamax:task.
  3. **STEER** — the Job is still running. Run `"$PY" -m chinamax steer <id>`
     (message on the stdin heredoc). Send NOTHING; keep polling.
  4. **RESUME** — the Job has ended. Run `"$PY" -m chinamax resume <id>` (message
     on the stdin heredoc); poll the NEW Job id it prints and track it as your id.
  Unsure between cancel and steer → STEER (a wrong steer is recoverable, a wrong
  cancel is not).
- **Steer/terminal race.** If `steer` reports the message was NOT delivered (the
  Job went terminal, exit 1 pointing at resume), re-route it to `resume` carrying
  the ORIGINAL message as the resume prompt, and disclose the possible duplicate
  inside the source Job's terminal relay.
- **Seam refusals ride one relay.** Any other refusal (lineage still running, not
  resumable) is relayed once, verbatim, as your ONE SendMessage(to='main'); never
  retry a refused verb.
- **Bounded failures never spin.** Exit 1, an unresolvable interpreter, or a poll
  killed by the Bash timeout is reported ONCE and ends that Job's relay.

## How the Bridge maps this onto the CLI seam

The Bridge normalizes `$ARGUMENTS` onto the seam argv (`python -m chinamax`):

- `profile=<name>` → `task --profile <name>`. Required on a fresh dispatch.
- `model=<string>` → `task --model='<string>'` (attached `=` form, value
  single-quoted — shipped-style strings carry `[..]` glob characters, and the
  attached form survives a leading `-`). Optional; omitted ⇒ the Profile's
  default model. Pinned to the Thread — resume never changes it.
- `--read-only` → `--read-only`. Write-capable is the default; this is the opt-out.
- `bash_timeout=<seconds>` → `--bash-timeout-s <seconds>` (non-numeric refused).
- `poll=<seconds>` → the poll-loop `--timeout-ms <seconds×1000>` (non-numeric
  refused); it is NOT a `task` flag and is never passed to the `task` verb.
- The natural-language task text is the prompt, delivered on STDIN — never as
  argv — so quotes, newlines, `$(…)`, and leading dashes arrive byte-identical.

A dispatch is ALWAYS a fresh Bridge + fresh Job (`profile=` required). There are no
routing controls: continuing a Thread is the live Bridge's own act when the
operator follows up on a finished Job, and a new task, a different model string,
or a new Profile is a new /chinamax:task, hence a new Bridge.
