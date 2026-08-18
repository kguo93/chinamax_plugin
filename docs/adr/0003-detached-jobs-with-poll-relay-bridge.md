# Every dispatch detaches; the Bridge is a quiet long-poll relay

**Amended 2026-08-06.** The detached Runtime is shared by Claude and Codex. The
Claude Bridge remains a thin adapter, while the Codex Bridge uses native
underscore-safe Agent names and Terra/low/no-fork settings.

Codex's companion defaults to a foreground blocking run and only detaches on `--background`. We instead detach every dispatch immediately into a durable Job and keep the Bridge Agent alive as a poll-relay (bounded `status --wait` calls in a loop) that accepts mid-run messages. This gives normal-subagent interactivity without ever tying a Job's survival to the Bridge: if the Bridge or the Claude session dies, the Job runs on and remains recoverable via `/chinamax:status` and `/chinamax:result`.

**Amended 2026-07-24** on live transcript evidence (session `95ec6400`): the original design — 90 s polls plus free progress relay via a "background, addressable" spawn — let the harness create a generic sonnet teammate wrapper that re-spawned the Bridge unnamed beneath itself: three Claude layers, none honoring the `model: haiku` frontmatter, ~40 poll turns/hour, and seven mid-run main-context wakes for one Job. The relay is now: exactly ONE named Bridge teammate per dispatch, spawned with an explicit `model: haiku` override in the Agent call and the full Bridge contract carried in the spawn prompt (named teammate spawns demonstrably ignore agent-frontmatter model and tools); the Bridge is FORBIDDEN to spawn any subagent; the long-poll defaults to 900 s (`--timeout-ms 900000`, Bash call timeout set above it), overridable per dispatch via `poll=<seconds>`; and mid-run the Bridge relays errors only — no progress messages — with `/chinamax:status` and the Stop-hook notice covering on-demand visibility.

**Amended 2026-07-27**: the relay channel is now explicit and exactly-once. The Bridge runs as a background teammate, so nothing it prints reaches the operator — every relay MUST be a `SendMessage(to='main')` tool call, and the contract fires EXACTLY ONE per relayed Job: at terminal, never before. The Job-id acknowledgment was dropped and progress stays silent, so the sequence is operator → Bridge → worker → Bridge → `SendMessage(to='main')` → main, with the main agent regurgitating the relayed content verbatim; the one message carries the worker's response untouched for a completed Job, the status/errorMessage otherwise, and bounded failures ride the same single relay. Rejected: mandating the SendMessage only for the terminal result (leaves failure relays unspecified — a dead dispatch would end in silence) and keeping the id acknowledgment (a second message per Job for information `/chinamax:status` already serves).

**Amended 2026-07-30**: the Bridge is now a PERSISTENT teammate — one per Thread, named `chinamax-<profile>-<task-slug>` (human-readable slug), serving the dispatch Job and every resume-created Job of that Thread for the session's life. Operator messages reach it only by explicit addressing (teammate name, Profile, or "the bridge/worker"), forwarded by main as `SendMessage`; the Bridge classifies each one — steer while its Job is active, resume once it is terminal, cancel on explicit abandon intent (ambiguity reads as steer: a wrong steer is recoverable, a wrong cancel is not), and a refusal pointing at `/chinamax:task` for asks it cannot honor inside its Thread (another Profile, a fresh unrelated run) — always waits for the resulting Job to end, and fires the same exactly-one `SendMessage(to='main')` per Job, verbatim. A refusal is the only other permitted message and belongs to no Job. The default long-poll drops from 900 s to 120 s (Bash timeout 180 s) so a mailbox message is picked up within ~2 minutes — this replaces the deleted in-turn `/chinamax:steer` command (ADR 0008). `task` loses its `--resume`/`--fresh` routing controls: a fresh dispatch is always a new Bridge + Thread (Profile required, ADR 0006), and resume is exclusively the live Bridge's act on its own lineage — `resume <explicit-id>` now refuses only when its own lineage is still active, not workspace-wide. The 2026-07 sentence "if the Bridge or the Claude session dies, the Job runs on" no longer holds: per ADR 0004 (reversed 2026-07-30) a Job dies with its session. Because no hook event fires when a teammate receives a message, the contract is enforced by a PreToolUse hook injecting a compact classification contract as subagent-scoped additionalContext on every Bridge seam call, alongside the spawn-prompt contract (ADR 0010).

**Amended 2026-07-31** (bridge-death reap): the last half of the original "if the Bridge or the Claude session dies, the Job runs on" no longer holds — the session half was retired 2026-07-30 (ADR 0004), and now the Bridge half is too. A Job must not outlive its Bridge, so the long-poll doubles as a supervision heartbeat: each `status --wait` on an active Job stamps `supervisedAt`/`supervisionTimeoutMs`, and the session hooks reap a Job whose heartbeat has aged past `2×bound + 10 s` as `interrupted` (`bridge terminated`; ADR 0004 amended). The contract's bounded-failure rule (`commands/task.md`: an exit 1 or a Bash-timeout-killed poll "is reported ONCE and ends that Job's relay") therefore now forfeits that Job ~`2×bound + 10 s` later via the same staleness reap — a Bridge that stops polling is deliberately indistinguishable from a dead one — and the contract text itself stays unchanged. Rejected alternative: keying detection on a `SubagentStop` hook event — live probing (4 instrumented sessions, 2026-07-31) proved `SubagentStop` fires at every healthy teammate turn-end, does NOT fire on a mid-turn TaskStop kill, and never carries the teammate name, so it cannot distinguish a dead Bridge from a live one.

**Amended 2026-08-07** (refusals removed): the 2026-07-30 amendment above gave the
Bridge a fourth classification outcome — "a refusal pointing at `/chinamax:task` for
asks it cannot honor inside its Thread (another Profile, a fresh unrelated run)" — and
stated "A refusal is the only other permitted message and belongs to no Job." That
refusal branch is REMOVED. The Bridge now classifies every exact addressed follow-up
as ONLY cancel, steer, or resume, and always acts on it — it never refuses an operator
ask. `skills/chinamax-bridge/SKILL.md` steps 6–7 and the `commands/task.md` spawn copy
drop the out-of-scope/refusal wording (`test_bridge_contract.py` and
`test_task_command.py` updated to match). Input/transport validation is UNCHANGED and
is not a Bridge refusal: a malformed `model=` token (spaces/quotes), an empty prompt,
and duplicate flags are still rejected at the grammar seam, and the profile-required
rule at `/chinamax:task` still holds (ADR 0006). A mid-Thread ask that the old refusal
covered — switch Profile/model, or a new unrelated task — is now folded into the
Thread as a steer/resume instead of refused; the pinned model and by-name Profile
still govern the wire (ADR 0006, amended 2026-08-07), so such an ask is carried into
the worker's Thread but does not itself switch the Thread's model or Profile.

**Amended 2026-08-18** (Bridge model reference): the `model: haiku` spawn
override in the 2026-07-24 amendment above is historical evidence, not current
policy — per ADR 0006 (amended 2026-08-18) the Claude Bridge now spawns with an
explicit `model: "sonnet"` override. The relay mechanics in this ADR are
unchanged.
