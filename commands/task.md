---
description: Dispatch a task to a non-Claude worker model (deepseek, mimo, glm, minimax, kimi) through the chinamax Bridge Agent — it detaches a durable Job, long-polls it, relays errors and the terminal result, and forwards mid-run messages as steers.
argument-hint: "profile=<name> [--read-only] [--resume|--fresh] [bash_timeout=<seconds>] [poll=<seconds>] <what the worker model should do>"
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
- a per-dispatch `name` (e.g. `chinamax-<short-slug>`), so the running Bridge is
  addressable and a mid-run message can be forwarded to it as a steer.
- BACKGROUND, addressable — not foreground: a foreground subagent cannot receive
  a message while it runs, and forwarding a mid-run Steer is the whole point.

The Bridge's terminal SendMessage(to='main') carries the worker's response. Your
final user-visible response is that relayed content REGURGITATED VERBATIM — as
if the operator had dispatched the worker model themselves in their own
terminal. Do not paraphrase, summarize, verify, trim, reorder, or add commentary
of your own. The chain is: operator → you → Bridge → worker → Bridge →
SendMessage(to='main') → you → operator, and a break in ANY link is a failed
dispatch.

Raw dispatch request:
$ARGUMENTS

## Bridge contract — embed this verbatim in the spawn `prompt`

Because a named spawn ignores the agent frontmatter, the full contract travels in
the `prompt` of the Agent call. Carry these rules into it:

- **Never spawn anything.** The Bridge is FORBIDDEN to spawn any subagent, under
  any circumstances — it has no Agent tool and must never obtain one, wrap itself
  in another teammate, or re-dispatch itself. Exactly one named Bridge serves the
  dispatch; there is nothing beneath it. It does the plumbing with Bash and the
  seam, never by delegating.
- **Resolve the interpreter once.** Take the first absolute executable of: the
  path `/chinamax:setup` recorded, then `$CHINAMAX_PYTHON`, then
  `~/miniconda3/envs/chinamax/bin/python`; only as a last resort
  `conda run -n chinamax python`. Reuse it as `$PY` for every seam call.
- **Transport prompt and steer text on STDIN via a quoted heredoc** so quotes,
  newlines, `$(…)`, backticks, and leading dashes arrive byte-identical and no
  subshell runs. Pass NO prompt words on argv.
- **A fresh `task` REQUIRES a Profile** (`profile=<name>`, from the mapping
  below). With none, REFUSE and list the five shipped Profiles (deepseek, mimo,
  glm, minimax, kimi) — there is no default. A `resume` takes no `profile=`.
- **Refuse, making no seam call**, when `--resume` and `--fresh` are both given,
  more than one `profile=` is given, `bash_timeout=<v>` or `poll=<v>` is
  non-numeric, or the dispatch text is empty.
- **Poll loop.** Repeat `"$PY" -m chinamax status <id> --wait --timeout-ms 900000`
  with the Bash tool call's own `timeout` set to 960000 ms (above the seam
  bound). Branch on the EXIT CODE: 0 terminal (fetch the result), 2 still active
  (poll again — every non-terminal return is 2), 1 a bounded failure (report once
  and stop). If the operator gave `poll=<seconds>`, use
  `--timeout-ms <seconds×1000>` and Bash `timeout` `(seconds+60)×1000` ms.
- **Stay silent while the Job runs.** Send NO progress messages — not the Job
  id, not a phase change, not a log line; a successful steer is silent.
- **Relay with EXACTLY ONE SendMessage(to='main') at terminal.** The Bridge runs
  in the background, so nothing it prints reaches the operator: to relay, it
  MUST call the SendMessage tool with to='main'. Ending its turn without calling
  SendMessage(to='main') is NOT a relay and the operator will never see it.
  Exactly one SendMessage(to='main') per relayed Job — when the Job ends, never
  before: the worker's response for a `completed` Job; the status and
  `errorMessage` for a failed/cancelled/interrupted one; a bounded failure
  reported once.
- **Relay the response untouched.** Run `"$PY" -m chinamax result <id>`; its
  first line is the `<id>  <status>` header, and for a `completed` Job
  everything after it is the worker's complete final answer. Strip the header
  line and relay the response UNTOUCHED — byte-for-byte verbatim: no omission,
  summary, verification, judgment, correction, added content, or reformatting.
  The seam's stored result is unchanged; this is presentation only.
- **Steer when busy, resume when finished.** A mid-run message becomes
  `"$PY" -m chinamax steer <id>` (message on stdin); after terminal it routes to
  `"$PY" -m chinamax resume <id>` (explicit id). If `steer` reports the message
  was NOT delivered (the finish-during-steer race, exit 1 pointing at resume),
  re-route to `resume` carrying the ORIGINAL message as the resume prompt, and
  disclose the possible duplicate inside the source Job's terminal relay.
- **Bounded failures never spin.** Exit 1, an unresolvable interpreter, or a poll
  killed by the Bash timeout is reported ONCE and ends the relay.

## How the Bridge maps this onto the CLI seam

The Bridge normalizes `$ARGUMENTS` onto the seam argv (`python -m chinamax`):

- `profile=<name>` → `task --profile <name>`. Required on a fresh dispatch.
- `--read-only` → `--read-only`. Write-capable is the default; this is the opt-out.
- `bash_timeout=<seconds>` → `--bash-timeout-s <seconds>` (non-numeric refused).
- `poll=<seconds>` → the poll-loop `--timeout-ms <seconds×1000>` (non-numeric
  refused); it is NOT a `task` flag and is never passed to the `task` verb.
- The natural-language task text is the prompt, delivered on STDIN — never as
  argv — so quotes, newlines, `$(…)`, and leading dashes arrive byte-identical.

`--resume` and `--fresh` are Bridge-level ROUTING controls, NOT `task` flags:

- `--fresh` (or a plain first dispatch) routes to the `task` verb.
- `--resume` (or a natural-language follow-up) routes to the `resume` verb, which
  continues the prior Thread and takes no `profile=`.

Leave `--resume`/`--fresh` in the forwarded request; the Bridge does the routing
and never passes them through to a seam verb as an argument.
