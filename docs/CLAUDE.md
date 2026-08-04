# docs/ — conventions

This file is **conventions only**: how to format an ADR, and — the heart of it — the
**routing tables** that say which ADR(s) to **read** before a change and which to **amend**
after it. Inventory (the file list and the full ADR table) lives in the sibling
`./repo-map.md`; domain vocabulary lives in `../CONTEXT.md`.

Root `CLAUDE.md` is the entry point: read `CONTEXT.md` and the relevant `docs/adr/*.md`
before building anything, **prefer amending an existing ADR over creating a new one**, and
amend an ADR (and `CONTEXT.md` when a glossary term shifts) whenever a decision contradicts
it. This file makes "the relevant ADRs" concrete.

Ground truth is the **product code + active manifests** (`src/chinamax/`, `.claude-plugin/`,
`hooks/hooks.json`, `agents/`, `commands/`, `skills/`). An ADR records the *decision of
record* and its rejected alternatives; where an ADR's prose and the code disagree,
reconcile by amending the ADR or fixing the code — never by trusting stale prose.
`docs/verification-report.md` is a recorded run report, not authority.

## ADR formatting rules

- **Filename**: `NNNN-kebab-slug.md`, four-digit zero-padded, sequential, no gaps. The next
  number is the highest in `adr/` plus one (currently → `0013`).
- **Heading**: a bare prose title, e.g. `# One Bridge Agent, providers as Profiles, no
  default` (match the existing ADRs — no leading number).
- **Amend in place; never fork a decision** (root `CLAUDE.md`): when a later decision changes
  an ADR, add a dated `**Amended <date>**` paragraph *in that same file*. When the change
  **reverses** the original, quote the original decision before overriding it (precedent:
  ADR 0004, reversed 2026-07-30). If the slug now contradicts the content, `git mv` it to a
  truthful slug. **Never create a new ADR file for a reversal or amendment.**
- Create a *new* ADR only for a decision genuinely orthogonal to every existing one.

## Read routing — which ADR to read for which purpose

Match your change to a row and read that ADR (plus `../CONTEXT.md`) **before** editing. The
twelve ADRs group into five themes.

### 1 · Runtime & provider wire
| If your change / question touches… | Read | Governs |
|---|---|---|
| How the loop talks to a provider — the Anthropic Messages wire format via the `anthropic` SDK, why not OpenAI chat-completions | **0001** | `src/chinamax/provider.py`, `src/chinamax/data/profiles.json` |
| Supervision model — no wall-clock/turn caps; per-API-call inactivity + retry ladder; per-bash-command timeout fed back as an observation | **0002** | `src/chinamax/liveness.py`, `src/chinamax/loop.py` |
| Environment & dependencies — the dedicated `chinamax` conda env (never `py_automation`), official `anthropic` SDK | **0009** | `src/chinamax/doctor.py`, `pyproject.toml`, `scripts/_interpreter.sh` |

### 2 · Job lifecycle, supervision & Bridge orchestration
| If your change / question touches… | Read | Governs |
|---|---|---|
| The Bridge contract — detach + quiet long-poll relay, exactly-one `SendMessage(to='main')`, the poll loop, Bridge naming/spawning, and the **bridge-death heartbeat reap** | **0003** | `agents/chinamax.md`, `commands/task.md`, `src/chinamax/__main__.py` (`_status_wait`), `src/chinamax/state.py` (`stamp_supervision`, `reap_stale_supervision`) |
| Job / session ownership — SessionEnd kills the ending session's Jobs, SessionStart reaps dead-session orphans, the session registry, the bridge-death sweep | **0004** | `hooks/hooks.json`, `src/chinamax/hooks/session_start.py` + `session_end.py`, `src/chinamax/state.py` (`reap_session` / `reap_orphans` / `reap_stale_supervision`, session registry) |
| Providers-as-Profiles — a single `chinamax` Bridge Agent, per-dispatch pinned model (pro-only reversed 2026-08-03), no default, adding a provider/tier, Bridge instance naming | **0006** | `agents/chinamax.md`, `src/chinamax/profiles.py`, `src/chinamax/data/profiles.json`, `src/chinamax/spec.py`, `src/chinamax/state.py` (`new_record`/`create_resume`) |
| Mid-run steering — the per-Job steer queue, drained/injected at loop-iteration boundaries | **0008** | `src/chinamax/loop.py` (`_drain_steers`…), `src/chinamax/state.py` (steer helpers), `src/chinamax/__main__.py` (`run_steer`), `src/chinamax/transcript.py` |

### 3 · Confinement & safety
| If your change / question touches… | Read | Governs |
|---|---|---|
| Tool-layer file/bash safety — realpath containment, cwd-pinned bash + denylist + timeouts, write-tool disabling for read-only Jobs (not an OS sandbox) | **0005** | `src/chinamax/confinement.py`, `src/chinamax/tools/` |

### 4 · Results & duplication guard
| If your change / question touches… | Read | Governs |
|---|---|---|
| Result fidelity — the worker's `report_result` self-report, envelope stripped, prose relayed verbatim, no runtime audit layer | **0007** | `src/chinamax/tools/report_result.py`, `src/chinamax/loop.py`, `src/chinamax/__main__.py` (`_print_result`), `skills/chinamax-results/` |
| Duplication guard — soft measures only: contract language, a non-blocking Stop notice, live-Bridge roster injection, the PreToolUse contract (never a hard block) | **0010** | `src/chinamax/hooks/stop.py` + `user_prompt.py` + `bridge_contract.py`, `commands/task.md`, `skills/chinamax-results/` |

### 5 · Tests & install
| If your change / question touches… | Read | Governs |
|---|---|---|
| Test harness — the hermetic fake Anthropic-Messages provider server; no API keys | **0011** | `tests/` |
| Install source — GitHub canonical, rpi4 backup mirror, marketplace/manifest/versioning, git remotes | **0012** | `.claude-plugin/`, `src/chinamax/doctor.py` (`source_repo_path`) |

## Edit routing — which ADR to amend when a decision changes

- **A change inside one of the five themes** → amend that theme's ADR in place (dated
  `**Amended <date>**` paragraph), per the ADR formatting rules above.
- **A reversal** (the new decision contradicts the old) → quote the original, then override,
  in the SAME file; `git mv` the slug if it now lies. (Precedent: ADR 0004.)
- **A change spanning two ADRs** → amend the primary one and cross-reference the other inline
  by number (the ADRs already do this — e.g. 0003 ↔ 0004 / 0008 / 0010, 0007 ↔ 0010).
- **A grilling or decision that contradicts an existing ADR** → edit that ADR (and
  `CONTEXT.md` if a glossary term shifts); never leave a stale decision beside the new one.

## Adding or amending an ADR — checklist

An ADR change is not done until:
1. (New ADR) the next sequential number is used — no gaps, no reuse.
2. The `adr/` table in `./repo-map.md` is updated (number, filename, title/scope).
3. This file's **Read routing** and **Edit routing** rows are added or adjusted.
4. If it supersedes or narrows another ADR, that ADR's status/amendment note and its routing
   rows here are updated too.

## `agents/` — how to use it

`agents/` holds this repo's issue-tracker and domain-modeling conventions — conventions to
follow, not templates to copy. Read the relevant file first when doing that kind of work:

- **Issue-tracked feature work** (writing a PRD, filing or working an issue) →
  `agents/issue-tracker.md`.
- **Triaging or labeling an issue** → `agents/triage-labels.md` (use its canonical label
  strings so labels stay consistent).
- **ADR / domain-model work** → `agents/domain.md` (read `../CONTEXT.md` and the ADRs
  touching your area first, name concepts with the glossary's vocabulary, and flag any output
  that contradicts an ADR rather than silently overriding it).
