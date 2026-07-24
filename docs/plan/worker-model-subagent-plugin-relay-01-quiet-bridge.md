# Plan — relay-01: one quiet haiku Bridge, 900 s configurable poll, steer command

## Read first (handoff block)

- `docs/adr/0003-detached-jobs-with-poll-relay-bridge.md`, `0007-self-reported-results.md`, `0008-steer-queue.md`, `0010-duplication-guard.md` — each carries an **Amended 2026-07-24** paragraph; those amendments are the authoritative decisions this plan implements.
- `CONTEXT.md` — updated Bridge Agent and Steer definitions; use these terms.
- `CLAUDE.md` — the "2026-07-24 relay redesign" bullet (decision summary) and the conda-env test commands.
- `agents/chinamax.md` and `commands/task.md` — the contracts being rewritten.
- `commands/resume.md`, `commands/cancel.md`, `commands/status.md` — the `!`-launcher patterns the new steer command must mirror (resume for prompt transport, cancel for bare-id resolution).
- `tests/repo-map.md` — which test modules cover the bridge contract and the command surface.
- Motivating evidence (do not re-derive): session `95ec6400` transcripts showed a named spawn of `chinamax:chinamax` became a generic sonnet teammate (frontmatter `model`/`tools` ignored), which re-spawned the Bridge unnamed beneath itself and sent 7 mid-run messages to the main context.

## Locked decisions (do not re-litigate)

1. Exactly ONE Claude subagent per dispatch: a NAMED `chinamax:chinamax` Bridge teammate. The Bridge is FORBIDDEN to spawn any subagent — no Agent tool use, ever, under any circumstances.
2. The Agent call in `commands/task.md` passes `model: "haiku"` EXPLICITLY and carries the full Bridge contract in the spawn `prompt` (named spawns get a generic system prompt, so agent frontmatter cannot be relied on).
3. Long-poll default 900 s: `status <id> --wait --timeout-ms 900000`, with the Bash tool call's own `timeout` parameter set to 960000 ms. Per-dispatch override `poll=<seconds>` (non-numeric value → refusal, same rule as `bash_timeout`); maps to `--timeout-ms <seconds*1000>` with Bash `timeout` = `(seconds+60)*1000`.
4. Mid-run relay policy: the Bridge messages the operator ONLY for errors (bounded failures; steer-delivery-failure disclosure when re-routing to resume). Zero progress messages. The terminal result is always relayed. A successful steer forward is silent.
5. Result presentation: strip report scaffolding — status headers, the `report_result` envelope, "task completed" boilerplate — and fix layout; the worker's own sentences stay untouched. No omission, summarization, verification, judgment, or added content. Runtime storage stays verbatim; this is Bridge-side presentation only.
6. New `/chinamax:steer` command: thin `!`-launcher over the seam's `steer` verb. Message text required; Job id optional — a bare steer targets the single active Job, several active Jobs are listed rather than guessed (mirror cancel). Prompt/message transport mirrors `commands/resume.md`.

## Changes by file

1. `commands/task.md` — rewrite. Keep `allowed-tools: Agent` and inline execution. Instruct exactly one Agent call: `subagent_type: "chinamax:chinamax"`, a per-dispatch `name`, explicit `model: "haiku"`, background/addressable. The `prompt` embeds the full Bridge contract: no-spawn prohibition, interpreter resolution, STDIN quoted-heredoc transport, profile-required and argument-conflict refusals, the 900 s poll loop with `poll=` override and explicit Bash timeouts, exit-code branching, errors-plus-terminal-only relay policy, envelope-strip result presentation, steer forwarding with the steer/terminal race rule, resume routing, bounded failure modes. Add `poll=<seconds>` to the argument-hint.
2. `agents/chinamax.md` — update in place: poll section `--timeout-ms 90000` → `--timeout-ms 900000` plus the `poll=` override and the explicit Bash-call `timeout` guidance; delete the progress-line paragraph and state the errors-plus-terminal-only policy; replace the verbatim-result section with the envelope-strip rules; add the absolute no-spawn prohibition to the forbidden section; keep everything else (interpreter resolution, heredocs, refusals, routing, bounded failures), adjusting the "poll killed by the Bash tool timeout" retry rule to the new timeouts.
3. `commands/steer.md` — NEW, per locked decision 6.
4. `skills/chinamax-results/SKILL.md` — align its verbatim-relay language with the amended ADR 0007: envelope stripped, worker prose untouched, never redone or judged.
5. Tests — update `tests/test_bridge_contract.py` and the task-command surface test module (find it via `tests/repo-map.md`): assert the 900000 default and `poll=` mapping text, the explicit `model: "haiku"` in `commands/task.md`, the errors-only relay phrase, the envelope-strip phrase, and the no-spawn phrase present in BOTH `agents/chinamax.md` and the `commands/task.md` prompt block (lockstep assertions so the two contracts cannot drift). Add a surface test for `commands/steer.md` following the existing command-test pattern.
6. `README.md` — commands table gains `/chinamax:steer`; add the `poll=` flag; update the relay-policy and result-presentation descriptions.
7. `CLAUDE.md` — update the Commands bullet (add steer) and the poll-relay bullet; rewrite the "2026-07-24 relay redesign" bullet's "implementation pending" marker to implemented, keeping the decision summary.
8. `repo-map.md` (root) — the `commands/` line gains `steer`; confirm the `docs/plan/` line counts this plan. `tests/repo-map.md` — update if the test inventory changed.
9. `src/` — NO Runtime changes expected. Exception: check the `status --wait` / `--timeout-ms` handling in the CLI for any clamp below 900000; lift the clamp if present, leaving the seam's 240 s default unchanged.

## Verification

- `conda run -n chinamax python -m pytest /home/klg2138/deepseek_plugin/tests -q` → all green.
- `git status --short` → no stray or unexplained files beyond the scoped set.

## Commit (the implementer commits)

- Branch: `master` (repo practice: direct commits).
- Stage ONLY: `commands/`, `agents/chinamax.md`, `skills/chinamax-results/`, the changed files under `tests/`, `README.md`, `CLAUDE.md`, `repo-map.md`, `CONTEXT.md`, `docs/adr/0003*.md`, `docs/adr/0007*.md`, `docs/adr/0008*.md`, `docs/adr/0010*.md`, `docs/plan/worker-model-subagent-plugin-relay-01-quiet-bridge.md`.
- Do NOT stage: `docs/verification-report.md`, the `.scratch/` deletions, or any other pre-existing working-tree change.
- Message style: `relay-01: one quiet haiku Bridge, 900s configurable poll, steer command` plus the standard trailers.

## Out of scope

- Runtime loop or tool changes, hooks, Profiles, live provider dispatches, SessionStart/Stop hook changes.
