---
description: Dispatch a task to a non-Claude worker model (deepseek, mimo, glm, minimax, kimi) through the chinamax Bridge Agent — it detaches a durable Job, poll-relays progress, and forwards mid-run messages as steers.
argument-hint: "profile=<name> [--read-only] [--resume|--fresh] [bash_timeout=<seconds>] <what the worker model should do>"
allowed-tools: Agent
---

Invoke the `chinamax:chinamax` subagent via the `Agent` tool
(`subagent_type: "chinamax:chinamax"`), forwarding the dispatch below.

This command runs INLINE so the `Agent` tool stays in scope — a forked
general-purpose subagent does not expose it. Dispatch the Bridge as a
BACKGROUND, addressable agent, not foreground: a foreground subagent cannot
receive a message while it runs, and the whole point of the Bridge is that a
mid-run message can be forwarded to the running Job as a steer.

The final user-visible response must be the Bridge's relayed output — the
worker's result verbatim. Do not paraphrase, summarize, or add commentary.

Raw dispatch request:
$ARGUMENTS

## How the Bridge maps this onto the CLI seam

Pass `$ARGUMENTS` to the Bridge as-is; the Bridge normalizes it onto the seam
argv (`python -m chinamax`). For reference, the mapping the Bridge applies:

- `profile=<name>` → `task --profile <name>`. Required on a fresh dispatch; with
  no Profile the Bridge refuses and lists the shipped Profiles (deepseek, mimo,
  glm, minimax, kimi). There is no default.
- `--read-only` → `--read-only`. Write-capable is the default; this is the opt-out.
- `bash_timeout=<seconds>` → `--bash-timeout-s <seconds>` (a non-numeric value is
  refused).
- The natural-language task text is the prompt, delivered on STDIN — never as
  argv — so quotes, newlines, `$(…)`, and leading dashes arrive byte-identical.

`--resume` and `--fresh` are Bridge-level ROUTING controls, NOT `task` flags:

- `--fresh` (or a plain first dispatch) routes to the `task` verb.
- `--resume` (or a natural-language follow-up) routes to the `resume` verb,
  which continues the prior Thread and takes no `profile=`.

Leave `--resume`/`--fresh` in the forwarded request; the Bridge does the routing
and never passes them through to a seam verb as an argument.
