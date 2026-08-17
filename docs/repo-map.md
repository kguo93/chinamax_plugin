# docs/ — inventory

Conventions live in `./CLAUDE.md` (ADR formatting rules + the Read/Edit ADR routing tables).
Domain vocabulary lives in `../CONTEXT.md`.

## Files & subdirectories

| Path | What it is |
|------|-----------|
| `CLAUDE.md` | docs/ conventions: ADR formatting + the Read/Edit ADR routing tables. |
| `AGENTS.md` | `@CLAUDE.md` stub, so Codex and other agents read the same conventions. |
| `repo-map.md` | This inventory. |
| `verification-report.md` | 0.4.3 verification scope and native-vs-mocked evidence. |
| `adr/` | Architecture Decision Records 0001–0016 (table below). |
| `agents/` | Issue-tracker & domain-modeling conventions: `issue-tracker.md`, `triage-labels.md`, `domain.md`. |

## adr/ — decision records

Themes match the `./CLAUDE.md` Read-routing sections. `†` = has dated amendments (read the
whole file, including its `**Amended <date>**` notes).

| ADR | File | Title | Theme |
|-----|------|-------|-------|
| 0001† | `0001-anthropic-messages-wire-format.md` | Runtime speaks Anthropic Messages, not OpenAI chat-completions | Runtime & wire |
| 0002† | `0002-liveness-based-supervision.md` | No wall-clock or turn caps; liveness-based supervision only | Runtime & wire |
| 0003† | `0003-detached-jobs-with-poll-relay-bridge.md` | Every dispatch detaches; the Bridge is a quiet long-poll relay | Job lifecycle & Bridge |
| 0004† | `0004-session-scoped-jobs.md` | Jobs are session-scoped — SessionEnd kills, SessionStart reaps orphans (reversed 2026-07-30) | Job lifecycle & Bridge |
| 0005† | `0005-tool-layer-confinement.md` | Confinement is tool-layer, not OS sandbox | Confinement & safety |
| 0006† | `0006-single-bridge-agent-with-profiles.md` | One Bridge Agent, providers as Profiles, no default (pro-only reversed 2026-08-03; per-dispatch pinned model) | Job lifecycle & Bridge |
| 0007† | `0007-self-reported-results.md` | Final results are the worker's self-report, envelope stripped, prose untouched | Results & duplication guard |
| 0008† | `0008-steer-queue.md` | Mid-run messages land in a steer queue | Job lifecycle & Bridge |
| 0009† | `0009-anthropic-sdk-in-dedicated-conda-env.md` | Official anthropic SDK in a dedicated conda env | Runtime & wire |
| 0010† | `0010-duplication-guard.md` | Duplication guard: contract language + Stop notice, no hard blocks | Results & duplication guard |
| 0011 | `0011-hermetic-fake-provider-tests.md` | Tests run against a hermetic fake provider server | Tests & install |
| 0012 | `0012-github-canonical-install-source.md` | GitHub is the canonical install source; rpi4 is a backup mirror | Tests & install |
| 0013† | `0013-dual-host-runtime-and-thin-adapters.md` | One Host-neutral Runtime with thin Claude and Codex adapters | Host architecture |
| 0014 | `0014-codex-yolo-runtime-boundary-and-lifecycle-gaps.md` | Codex yolo boundary and detached lifecycle backstop | Host architecture |
| 0015† | `0015-cross-platform-runtime-portability.md` | Native macOS/Windows portability with Linux preservation | Platform architecture |
| 0016† | `0016-worker-host-policy-enforcement.md` | Worker-side Host-policy enforcement: Policy hooks, Memory injection, Worker MCP (per-Host toggles, default OFF; `mcp=` removed; nested Codex hook schema corrected 0.7.3) | Worker Host-policy enforcement |
