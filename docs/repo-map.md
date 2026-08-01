# docs/ — inventory

Conventions live in `./CLAUDE.md` (ADR formatting rules + the Read/Edit ADR routing tables).
Domain vocabulary lives in `../CONTEXT.md`.

## Files & subdirectories

| Path | What it is |
|------|-----------|
| `CLAUDE.md` | docs/ conventions: ADR formatting + the Read/Edit ADR routing tables. |
| `AGENTS.md` | `@CLAUDE.md` stub, so Codex and other agents read the same conventions. |
| `repo-map.md` | This inventory. |
| `verification-report.md` | Recorded live-verification run report. |
| `adr/` | Architecture Decision Records 0001–0012 (table below). |
| `agents/` | Issue-tracker & domain-modeling conventions: `issue-tracker.md`, `triage-labels.md`, `domain.md`. |

## adr/ — decision records

Themes match the `./CLAUDE.md` Read-routing sections. `†` = has dated amendments (read the
whole file, including its `**Amended <date>**` notes).

| ADR | File | Title | Theme |
|-----|------|-------|-------|
| 0001† | `0001-anthropic-messages-wire-format.md` | Runtime speaks Anthropic Messages, not OpenAI chat-completions | Runtime & wire |
| 0002 | `0002-liveness-based-supervision.md` | No wall-clock or turn caps; liveness-based supervision only | Runtime & wire |
| 0003† | `0003-detached-jobs-with-poll-relay-bridge.md` | Every dispatch detaches; the Bridge is a quiet long-poll relay | Job lifecycle & Bridge |
| 0004† | `0004-session-scoped-jobs.md` | Jobs are session-scoped — SessionEnd kills, SessionStart reaps orphans (reversed 2026-07-30) | Job lifecycle & Bridge |
| 0005 | `0005-tool-layer-confinement.md` | Confinement is tool-layer, not OS sandbox | Confinement & safety |
| 0006† | `0006-single-bridge-agent-with-profiles.md` | One Bridge Agent, providers as Profiles, pro tiers only, no default | Job lifecycle & Bridge |
| 0007† | `0007-self-reported-results.md` | Final results are the worker's self-report, envelope stripped, prose untouched | Results & duplication guard |
| 0008† | `0008-steer-queue.md` | Mid-run messages land in a steer queue | Job lifecycle & Bridge |
| 0009† | `0009-anthropic-sdk-in-dedicated-conda-env.md` | Official anthropic SDK in a dedicated conda env | Runtime & wire |
| 0010† | `0010-duplication-guard.md` | Duplication guard: contract language + Stop notice, no hard blocks | Results & duplication guard |
| 0011 | `0011-hermetic-fake-provider-tests.md` | Tests run against a hermetic fake provider server | Tests & install |
| 0012 | `0012-github-canonical-install-source.md` | GitHub is the canonical install source; rpi4 is a backup mirror | Tests & install |
