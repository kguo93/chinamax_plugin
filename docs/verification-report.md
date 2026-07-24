# chinamax — Live Verification Report

Live acceptance gauntlet for the `chinamax` worker-model subagent plugin, run against the
real provider endpoints. The hermetic pytest suite (181 tests) was green before and after.

## Method and honest scope

- **Dispatch surface.** Runs were driven through the **CLI seam** (`python -m chinamax …`),
  not the installed Bridge Agent, because the plugin was intentionally never installed into the
  operator's real Claude Code config (only an isolated `mktemp` smoke during surface-01). The CLI
  seam invokes the **identical Runtime, providers, state store, and Job lifecycle** the Bridge
  wraps; the Bridge is a thin Bash forwarder over these same commands. Where the plan calls for
  "message a busy Bridge" (the Run 2 steer), the `steer` CLI verb was used — identical queue+drain.
- **Session restart (Run 3).** The mid-run "fresh session" was reproduced by invoking the real
  `scripts/session_start_hook` a fresh session runs on startup, with `cwd` at the workspace — it
  produces the same SessionStart digest a restarted session would inject.
- **State-root pinning.** Dispatch and all evidence commands ran in **one shell context** with
  `CLAUDE_PLUGIN_DATA` pinned, so the dispatch context and the evidence shell resolve the **same**
  state root by construction (the split the plan guards against cannot arise).

### Operator acceptance (2026-07-24)

During the 2026-07-24 nine-plan verification pass, the operator reviewed the four method
substitutions this section discloses — (a) runs 1–3 dispatched through the CLI seam rather than the
installed plugin surface, (b) the Run 2 steer sent via the `steer` CLI verb rather than by messaging
the busy Bridge, (c) the Run 3 session restart reproduced by invoking the real `session_start_hook`
script rather than an actual Claude-session restart, and (d) the state-root agreement read in one
pinned shell context rather than two independent readings — and ratified this report as accepted
evidence. No live re-run was required.

## Environment

- Workspace: `~/chinamax-verification` (clean dir, `git init`, seeded `app.py` + `slow_check.sh`).
- Baseline commit: `e57122c863f579fd4f021fdcb477dab2e4314cb2`
- Pinned state root (session == evidence shell): `~/.local/state/chinamax-gauntlet/state`
- Per-workspace state dir: `…/state/chinamax-verification-148498dfb87c2d4a`
- Preflight (`setup --json`): `ok: true`, env present, deps import, all five keys PRESENT,
  state writable. `profiles` showed correct endpoint/model/key for all five.

## Runs

| # | Profile | Job id | Duration | Outcome | Notes |
|---|---------|--------|----------|---------|-------|
| 1 simple | deepseek | `task-mryn41pq-2vgdxr` | ~16 s | completed | `hello.py` prints exactly `hello world` |
| 2 steer | deepseek | `task-mryn5tqs-7cxywt` | ~5 m 38 s | completed | steer drained mid-run; `[steer]` in Thread; headers on both files |
| 3 survival | deepseek | `task-mrynel0k-xwx0nd` | **83.0 min** | completed | 16/16 items; session-restart digest named the Job; never interrupted |
| — read | deepseek | `task-mryou7el-xbegyh` | ~23 s | completed | read a `CLAUDE.md` and reported all 6 facts correctly (grounded `read_file` call) |
| 4 smoke | kimi | `task-mryng0tk-5g50f1` | ~32 s | completed | `kimi.txt` = "Kimi" |
| 5 smoke | minimax | `task-mryng21e-pf379w` | ~20 s | completed | `minimax.txt` = "MiniMax-M3" |
| 6 smoke | glm | `task-mryoatno-zucx19` | ~27 s | completed | on corrected shipped model (see anomaly) — `glm_shipped.txt` = "GLM-4.6" |
| 7 smoke | mimo | `task-mryoauc3-hbfqb4` | ~15 s | completed | on corrected shipped model (see anomaly) — `mimo_shipped.txt` = "Claude" |

### Run 2 — steer evidence (from the Job's `thread.jsonl`)

The steer was sent during the `slow_check.sh` window and drained at the next turn boundary:

```
role=user  steer_id=1784879537308-0000-saf9cy.md  steer_ts=2026-07-24T07:52:17.308000+00:00
[steer] also add a comment header to every file you create, including any you already created
```

On disk afterward, both files carried a comment header — including `note_one.md`, which was
created **before** the steer, proving the steer took effect retroactively:

```
note_one.md: <!-- Step 1: Initial note for verification task -->
note_two.md: <!-- Step 2: Follow-up note after slow_check.sh completed -->
```

### Run 3 — 70+ min survival + session restart

- **Elapsed** from the record: `startedAt` 2026-07-24T07:58:15 → `completedAt` 2026-07-24T09:21:16
  = **83.0 min** (> 70 min floor).
- **All 16 items**: `checklist.log` holds exactly `item 1 done` … `item 16 done`; the self-report
  confirms all 16 `slow_check.sh` runs returned ok.
- **Session-restart digest** (real `session_start_hook`, cwd=workspace, captured at 08:25, run-3
  minute ~27) named the still-running Job:

  ```
  chinamax Jobs in chinamax-verification:
    task-mrynel0k-xwx0nd  running      running-tool      27m08s  deepseek  You have a checklist of 16 …
    … (completed smoke Jobs) …
    (+5 more)
  ```

  Corroborating `status`/`logs` at the same moment showed `running` at turn 12, never stopped.
- **Never interrupted**: stored status was only ever `running`→`completed` (`interrupted` is a
  read-side derivation, never stored). The derivation is conjunctive (worker-provably-gone AND
  heartbeat older than the 60 s grace); the worker was alive for the whole run, so it could never
  fire. Liveness was judged by the `updatedAt` heartbeat (daemon-refreshed ~30 s) sampled every
  60 s by a detached `setsid` sampler — status stayed `running` across all 83 samples. Max
  acceptable heartbeat gap: **60 s** (the stale grace); observed heartbeat interval ~30 s.

### Intermediate read test (deepseek)

deepseek read a workspace `CLAUDE.md` via its confined `read_file` tool and reported its contents.
All six ground-truth facts were reported correctly with no hallucination or omission (port 8347 /
reject 8080, UTC + ISO-8601, `purge_all()` test-only, retry ≤3× @ 250 ms, SQLite `data/widgets.db`,
no Postgres without an ADR, `wgt/` branch prefix), and the Thread confirms an actual `read_file`
call — the report is grounded, not guessed.

## Anomaly and fix — glm / mimo `[1m]` model strings

The first glm and mimo smokes **failed** with provider-side HTTP 400 (glm: "Unknown Model";
mimo: "Unsupported model") for the `[1m]` (1-million-context) model strings shipped verbatim from
implement-handoff. This is **not a plugin defect**: the Runtime dispatched, classified the 400 as
*permanent*, failed fast in one attempt (no wasted retries), and recorded the structured error
faithfully. deepseek, minimax (both `[1m]`) and kimi (no suffix) were accepted; only glm and mimo
reject the suffix.

**Fix (operator-directed, live-verified):** drop `[1m]` for glm (`glm-5.2`) and mimo
(`mimo-v2.5-pro`) in `src/chinamax/data/profiles.json`, with the matching `SHIPPED` constant in
`tests/test_profiles.py` and the README config table updated in lockstep. Validated first via the
`~/.claude/chinamax-profiles.json` overlay, then re-run against the corrected **shipped** file with
no overlay: both completed. The hermetic suite is green (181) after the change.

## Acceptance criteria

- [x] README suffices to install and configure on a fresh machine without reading source *(written; independently verified against the shipped code)*
- [x] deepseek simple dispatch and mid-run Steer verified with transcript evidence
- [x] 70+ min deepseek Job: no self-kill, no hang, progress relayed, state intact across a session restart mid-run *(83.0 min)*
- [x] Four smoke dispatches (kimi, minimax, glm, mimo) each return a structured result *(all four completed; glm/mimo on the corrected shipped model strings)*
- [x] Verification report committed with job ids, durations, anomalies

## Provider matrix (final)

| Profile | Shipped model (after fix) | Live result |
|---------|---------------------------|-------------|
| deepseek | `deepseek-v4-pro[1m]` | ✅ runs 1, 2, 3 + read test |
| kimi | `kimi-k3` | ✅ smoke |
| minimax | `MiniMax-M3[1m]` | ✅ smoke |
| glm | `glm-5.2` *(was `glm-5.2[1m]`)* | ✅ smoke on shipped file |
| mimo | `mimo-v2.5-pro` *(was `mimo-v2.5-pro[1m]`)* | ✅ smoke on shipped file |
