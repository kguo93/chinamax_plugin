# PRD — chinamax Surface (Claude Code plugin skin)

Scope: ADRs 0006, 0010. Part 3 of 3 (siblings: runtime, jobs).

## Problem Statement

With a Runtime and a Job supervisor in place, the operator still has no way to use them from Claude Code: no named subagent Claude can dispatch through the Agent tool, no slash commands, no session awareness of inherited Jobs, and no guard stopping Claude from redoing work a running Job already owns.

## Solution

The Claude Code plugin `chinamax`: a marketplace-installable package registering the Bridge Agent (`chinamax:chinamax`) — a thin forwarding wrapper that dispatches to the Job CLI, poll-relays progress, and forwards mid-run messages as Steers — plus eight slash commands, a SessionStart job-digest hook, a non-blocking Stop-notice hook, contract language preventing duplicated work, setup/install machinery, and the live verification proving a simple dispatch, mid-run steering, and a 70-minute survival run on deepseek with smoke dispatches on the other four Profiles.

## User Stories

1. As Claude, I want a named subagent `chinamax:chinamax` invocable through the normal Agent tool, so that dispatching a worker model feels identical to dispatching any registered subagent.
2. As the operator, I want to address the agent by name and assign implementation, investigation, testing, or review work with `profile=<name>` and optional flags, so that dispatching is one sentence.
3. As the operator, I want the Bridge to forward the task and then poll-relay progress (bounded `status --wait` loop), so that I see concise live updates while the Job stays independent (ADR 0003).
4. As the operator, I want a message sent to the busy Bridge forwarded as a Steer, so that interacting mid-run feels like messaging any normal subagent (ADR 0008).
5. As the operator, I want a message to a finished Bridge (or `/chinamax:resume`) to continue the most recent Thread, so that follow-ups keep full context.
6. As Claude, I want the Bridge contract to forbid me-the-Bridge from inspecting the repo, solving the task, or summarizing beyond relaying, so that the Bridge stays a thin wrapper (ADR 0010).
7. As the operator, I want a result-handling rule forbidding Claude-side substitute implementations when a Job fails or runs long, so that delegation is never silently undone (ADR 0010).
8. As Claude, I want a Stop-hook notice listing running Jobs, so that neither I nor the operator forgets in-flight work at turn end (ADR 0010).
9. As the operator, I want a SessionStart digest of running/recent Jobs in this workspace, so that a fresh session inherits awareness of a 70-minute Job mid-flight (ADR 0004).
10. As the operator, I want /chinamax:task, status, result, cancel, resume, logs, profiles, setup, so that every lifecycle action has a first-class command.
11. As the operator, I want /chinamax:setup to verify the conda env, dependencies, key file entries per Profile, and state-dir writability, offering to create the env if missing, so that first-run failures are diagnosed in one command.
12. As the operator, I want /chinamax:profiles to list configured Profiles with endpoint, model, and key presence (never key values), so that misconfiguration is visible at a glance.
13. As the operator, I want the repo to double as its own marketplace so `claude plugin marketplace add <path>` + `claude plugin install chinamax@deepseek-plugin` installs it, so that installation is two supported commands.
14. As the operator, I want installation and configuration documented (README covering install, profiles, keys, caps, commands, troubleshooting), so that a fresh machine can be set up without reading source.
15. As the operator, I want the live verification run — deepseek simple dispatch, mid-run Steer visible in the Thread, and a 70+ minute Job surviving un-killed and un-hung — plus one smoke dispatch per remaining Profile, so that the acceptance criteria of the original brief are demonstrated, not presumed.
16. As a maintainer, I want hook scripts and the Bridge dispatch path exercised by tests with crafted hook stdin and a fake CLI seam, so that session-lifecycle behavior is verified hermetically before any live run.

## Implementation Decisions

- Plugin manifest: `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` (NOT the repo root — the install command reads `<path>/.claude-plugin/marketplace.json`) listing the repo itself (repo = marketplace, marketplace name `deepseek-plugin`). Layout: agents/, commands/, hooks/, skills/, scripts/, src/, tests/.
- Bridge Agent definition: markdown agent with Bash-only tools, contract modeled on codex-rescue (single forwarding call; explicit prohibitions; flag mapping incl. `--read-only`, `--resume`/`--fresh`, bash-timeout override), extended with the poll-relay loop and Steer forwarding (deviations from Codex recorded in ADRs 0003/0008).
- One Profile per dispatch, named explicitly — the Bridge refuses profile-less dispatches (ADR 0006).
- Hooks: SessionStart (inject digest, export session id for provenance) and Stop (non-blocking notice); deliberately no SessionEnd hook (ADR 0004). Hook scripts are package entrypoints (resolved via the recorded env python) reading Job state through one shared tolerant enumeration seam — the identical seam the CLI uses, in-process rather than a subprocess at every turn end.
- Duplication guard is threefold (ADR 0010): Bridge contract, result-handling skill rule, Stop notice.
- Commands are thin markdown wrappers over the same CLI seam; result rendering returns the worker's report verbatim (ADR 0007).
- Live verification runs in a throwaway repo at `~/chinamax-verification/`; the 70-minute Job interleaves small edits with a ~5-minute sleep script between checklist items (cheap tokens, real duration).

## Testing Decisions

- Good tests exercise the plugin surface behaviorally: invoke real hook scripts with crafted JSON stdin and assert output/exit codes; drive the Bridge's dispatch path against the CLI seam with the fake provider; never test markdown prose — with one recorded exception: contract stanzas whose behavior is untestable without a live Claude session (the Bridge contract, command payload instructions) are asserted at prose level hermetically and proven behaviorally by the live gauntlet.
- Covered: SessionStart digest content (running vs recent vs none), Stop notice presence/absence, hook resilience when state dir is empty/corrupt, command argument plumbing, refusal paths (no profile, resume-while-active).
- Live verification is the final acceptance layer on top of the hermetic suite, per operator convention that behavioral hook deliverables get live verification with real invocations.
- Prior art: Codex plugin's hooks.json/session-lifecycle/stop-gate structure studied during design; sibling PRDs' fake provider and CLI seam reused.

## Out of Scope

- Review-gate machinery (Codex's stop-time review gate), /transfer, broker daemons — not requested.
- Per-provider generated agents (rejected: one Bridge Agent, ADR 0006); PreToolUse edit-blocking (rejected, ADR 0010).
- Runtime loop and Job store internals (sibling PRDs).

## Further Notes

The Bridge Agent's model line stays Claude-side default (sonnet, Codex parity) — the worker model is chosen by Profile, not by the Bridge's own model. Vocabulary per CONTEXT.md.
