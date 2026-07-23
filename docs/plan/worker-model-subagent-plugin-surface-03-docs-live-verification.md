> IMPLEMENTER: READ EVERY FILE BELOW IN FULL BEFORE WRITING ANY CODE.
> Do not infer or fill gaps — all authoritative context is here.
> - @/home/klg2138/deepseek_plugin/CONTEXT.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0002-liveness-based-supervision.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0004-jobs-outlive-claude-sessions.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0008-steer-queue.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0007-self-reported-results.md
> - @/home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-surface/PRD.md
> - @/home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-surface/issues/03-docs-live-verification.md
>
> The README documents, and the gauntlet drives, the surface these blocking slices ship.
> Their CLI signatures and state rules are INPUTS to this slice — read them, do not redesign them here.
> Citations below name the SECTION and BULLET, never a line number: these sibling plans are under
> concurrent revision and every line-number pointer written against them has already gone stale once.
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-surface-01-installable-bridge.md — Bridge Agent contract, plugin manifest, `/chinamax:task`, and the interpreter discovery order.
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-surface-02-commands-hooks-guard.md — the seven remaining commands, SessionStart/Stop hooks, and the `setup` doctor (its pinned `--json` schema and recorded python path).
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-jobs-01-durable-dispatch.md — the `task`/`status --wait`/`logs` argv, the state-root rule, the per-workspace layout, and the pinned status EXIT CODES.
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-jobs-02-lifecycle-verbs.md — `result`/`cancel`/`resume` selection semantics, the active-Job refusals, and `effective_status`/`interrupted`.
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-jobs-03-steer-queue.md — `steer` argv, its finished-Job refusal, and the `[steer]` transcript marker.
> - @/home/klg2138/deepseek_plugin/docs/plan/worker-model-subagent-plugin-runtime-03-liveness-ladder.md — a blocking slice named by the issue. The retry ladder is what carries run 3 through a transient 429/5xx across 75 minutes, and it defines the "ladder exhaustion" the contingency below invokes.

# Plan — Documentation and the live verification gauntlet

## Solution

Write the README (install, configuration, commands, troubleshooting) and run the live acceptance gauntlet in a throwaway repo: on deepseek — simple dispatch, mid-run Steer, and a 70+ minute survival Job, all three dispatched through the installed plugin surface — then one smoke dispatch per remaining Profile; record everything in a committed verification report.

## Implementation Decisions

- README.md sections: What it is (glossary terms); Install (marketplace add/install, conda env creation, `pip install -e`); Configuration (profiles.json rows, ~/.claude/chinamax-profiles.json override, model-keys.env names, per-dispatch flags: `profile=`, `--read-only`, `--bash-timeout-s`, `--resume`/`--fresh`); Commands (all eight); How Jobs live (state dir, no session reaping, steer, resume); Troubleshooting (setup doctor, common provider errors, interrupted Jobs). Document the timeout flag as `--bash-timeout-s` — that is the spelling jobs/01's `task` argv bullet pins, and the README is exactly where a variant spelling would become permanent.
- Gauntlet workspace: `~/chinamax-verification/` — a *clean* dir (refuse to proceed, exiting non-zero, if it already exists: a leftover `hello.py` silently satisfies run 1's on-disk assertion), `git init`, seeded with a trivial python project (`app.py`) and `slow_check.sh` (sleep 300), then an initial commit. The baseline commit is for human audit of what the runs touched — nothing machine-compares `changed_files` against git, which ADR 0007 explicitly declines to do. Record the absolute path and baseline commit sha in the report.
- Dispatch surface: runs 1–3 are dispatched through the *installed plugin* — the Bridge Agent (`chinamax:chinamax` via the Agent tool) or `/chinamax:task` — from a Claude session whose workspace is `~/chinamax-verification`. This is not cosmetic: per jobs/01's `state.py` bullet the state root is `$CLAUDE_PLUGIN_DATA/state` when that variable is set and `$XDG_STATE_HOME/chinamax` (default `~/.local/state/chinamax`) otherwise, and the per-workspace dir is keyed on the git toplevel of the workspace root; the SessionStart digest resolves *this* workspace the same way (surface/02's hook-workspace bullet). A bare-CLI dispatch from a different cwd would file the Job where the digest never looks, making run 3's session-restart criterion pass vacuously.
- **State-root pinning — the evidence shell and the dispatch context must resolve the same root.** Dispatch happens inside Claude Code (where `CLAUDE_PLUGIN_DATA` is typically set); the raw CLI evidence commands run in a plain shell (where it typically is not). Left unpinned, the Jobs land under the plugin root while every `status`/`result`/`steer`/`logs` lookup reads the XDG fallback and exits 1 "no match" — or the mirror-image failure, where dispatch lands in the fallback and the SessionStart hook reads the plugin root. So: run `setup --json` in BOTH contexts (via `/chinamax:setup` in the session, and in the evidence shell) and require the reported `state_root` to be identical before any paid dispatch; if they differ, export the session's `CLAUDE_PLUGIN_DATA` in the evidence shell. Record both readings in the report. This is the one assumption the gauntlet cannot afford to leave implicit.
- Preflight before spending: `setup --json` (env, deps, key entries per Profile, state-root writability, and the `state_root`/`workspace_state_dir` the run will use) and `profiles` (endpoint/model, key PRESENT for all five) must be clean before any paid dispatch — a missing key discovered 40 minutes into run 3 costs the whole run. Note the honest limit: `setup` reports key PRESENT/MISSING by name only, so a present-but-revoked key still passes preflight and surfaces as a fast first-dispatch failure.
- Run 1 (deepseek, simple): dispatch "create hello.py printing hello world, run it, report" — expect a completed result and `hello.py` present on disk **and printing exactly `hello world`**. Asserting the file merely exists and exits zero would pass on an empty file. The on-disk artifact is the authoritative assertion; `changed_files` is a self-report (ADR 0007 explicitly accepts under-reporting) and is checked for plausibility only, never used as the oracle.
- Run 2 (deepseek, steer): dispatch a task naming its two files explicitly — `note_one.md` then `note_two.md` — **with one `./slow_check.sh` run between them**, so there is a ~5 minute window in which the Job is reliably still running; steers to a finished Job are refused (jobs/03's refusal bullet) and a bare two-file task can terminate in seconds. Naming the files is what lets the on-disk assertion glob deterministically. **Send the steer by messaging the busy Bridge**, not by calling the `steer` CLI verb: "message to a busy Bridge becomes a Steer" is the surface behavior under acceptance here (surface/01's contract bullet), and the CLI path is already covered hermetically by jobs/03. Steer text: "also add a comment header to every file you create, including any you already created". Evidence is the `[steer]` user message in the Job's `thread.jsonl` (jobs/01's layout bullet, jobs/03's marker bullet) plus the header present on disk. Send the steer the moment the sleep begins — a late steer hits the finished-Job refusal, which the Bridge contract reroutes to `resume`, and that is a different code path than the one being verified.
- Run 3 (deepseek, 70+ min survival): dispatch a checklist task — **16** small numbered edits, with "after EVERY item, including the last, run `./slow_check.sh` in the foreground; wait for it to finish; never background it". 16 × 300 s = 80 minutes of sleep alone. The floor rests on the sleeps, not on uncontrolled API latency (the original 12-item shape yielded only 55–60 min and could not meet the criterion at all); 16 rather than 15 buys a full sleep of margin, so one skipped or batched item still clears 70 minutes instead of landing exactly on the bar. During the run: progress sampled every 60 s to a durable file by a `setsid` sampler that survives the session restart and self-terminates when the Job goes terminal; between minutes 25 and 45 deliberately exit the Claude session entirely, start a fresh one rooted at the same workspace, and capture the SessionStart digest naming the Job plus `status`/`logs` showing it never stopped. Terminal assertions: result completed with all 16 items; elapsed computed from the record's `startedAt`/`completedAt` exceeds 70 min; the Job never went `interrupted`.
- Judging "no hang" correctly: each foreground `sleep 300` produces ~5 minutes with no new log line, because the reporter emits at turn and tool boundaries. Per-minute log growth is therefore the WRONG liveness signal — a healthy Job looks stalled. Judge liveness by the record's `updatedAt` heartbeat (refreshed on a fixed interval by a daemon thread even when the loop blocks) and state the maximum acceptable heartbeat gap in the report; use the log to show the 16 items progressing, not to prove second-by-second liveness.
- Contingency: if run 3 fails on a provider fault (ladder exhaustion per runtime/03's ladder, or an outage) rather than a compliance shortfall, record the anomaly and re-dispatch once **into a clean workspace** — a retry inheriting the failed attempt's files lets the model skip already-done items and perform fewer sleeps, quietly invalidating the 70-minute proof. A second provider-side failure is reported as an anomaly against the runtime scope rather than silently retried further.
- Runs 4–7 (smokes): kimi, glm, minimax, mimo — dispatch "create <profile>.txt containing the model's name, report"; expect a structured completed result each plus the `.txt` on disk.
- Verification report at the absolute path `/home/klg2138/deepseek_plugin/docs/verification-report.md`, committed to the **plugin** repo (not the throwaway workspace the gauntlet cd's into): per run — profile, job id, wall-clock duration, outcome, anomalies; plus workspace path and baseline commit, BOTH state-root readings (session and evidence shell) and the per-workspace state dir, the run 2 transcript excerpt showing the `[steer]` message, the run 3 restart evidence (the hand-copied SessionStart digest plus the surrounding status samples), the run 3 elapsed figure, and an explicit pass/fail line per acceptance criterion. The evidence files themselves live under `$W/evidence/` in the throwaway workspace, so quote the load-bearing excerpts inline in the committed report rather than referencing paths that will not survive.

## Acceptance Criteria (from the issue)

- [ ] README suffices to install and configure on a fresh machine without reading source
- [ ] deepseek simple dispatch and mid-run Steer verified with transcript evidence
- [ ] 70+ min deepseek Job: no self-kill, no hang, progress relayed, state intact across a deliberate session restart mid-run
- [ ] Four smoke dispatches (mimo, glm, minimax, kimi) each return a structured result
- [ ] Verification report committed with job ids, durations, anomalies

## Tracking

- [ ] README.md (timeout flag documented as `--bash-timeout-s`)
- [ ] preflight clean (`setup`, `profiles` — all five keys PRESENT)
- [ ] state roots pinned: `/chinamax:setup` in the dispatching session and `setup --json` in the evidence shell report the same `state_root`
- [ ] gauntlet workspace prepared (clean dir, seeded, baseline commit)
- [ ] runs 1–2 (simple, steer) executed through the plugin surface + evidence captured
- [ ] run 3 (70+ min survival incl. session restart) executed + evidence captured
- [ ] runs 4–7 smokes executed
- [ ] docs/verification-report.md written + committed
- [ ] root `repo-map.md` updated for the new `README.md` and `docs/verification-report.md`

## Verification plan (run in main — this plan IS the live verification; hermetic suite must already be green)

```bash
set -o pipefail   # REQUIRED: every assertion below reads an exit code through a `tee` pipeline

# Interpreter, in surface/01's pinned order: setup's recorded path, then $CHINAMAX_PYTHON, then conda.
# Data root = $CLAUDE_PLUGIN_DATA else $XDG_STATE_HOME/chinamax — surface/02's pinned python-path record.
DATA="${CLAUDE_PLUGIN_DATA:-${XDG_STATE_HOME:-$HOME/.local/state}/chinamax}"
PY="$(cat "$DATA/python-path" 2>/dev/null)"
[ -x "$PY" ] || PY="${CHINAMAX_PYTHON:-$HOME/miniconda3/envs/chinamax/bin/python}"

# Hermetic gate. NOT `conda run` — surface/01 notes some versions swallow the child's exit code,
# which would report a red suite as green, and "hermetic suite already green" is this slice's premise.
"$PY" -m pytest /home/klg2138/deepseek_plugin/tests -q || { echo "hermetic suite red — stop"; exit 1; }

# --- Preflight: never open a paid gauntlet blind ---
"$PY" -m chinamax setup --json | tee /tmp/chinamax-setup-shell.json   # exits 1 unless ok
"$PY" -m chinamax profiles                                            # five Profiles, key PRESENT
# STATE ROOT PINNING — compare this shell's root against the one /chinamax:setup reports INSIDE the
# Claude session that will dispatch. They must match, or dispatch and evidence read different roots.
"$PY" -c "import json;d=json.load(open('/tmp/chinamax-setup-shell.json'));print('shell state_root:',d['state_root'])"
# Run /chinamax:setup in the dispatching session; if its state_root differs, export that session's
# CLAUDE_PLUGIN_DATA here before continuing. Record BOTH readings in the report.

# --- Clean workspace with a committed baseline ---
W="$HOME/chinamax-verification"
[ -e "$W" ] && { echo "REFUSING: $W exists — move it aside (a stale hello.py fakes run 1)"; exit 1; }
mkdir -p "$W/evidence" && cd "$W" && git init
git config user.email >/dev/null || git config user.email gauntlet@example.invalid
git config user.name  >/dev/null || git config user.name  gauntlet
printf 'def main():\n    print("seed")\n' > app.py
printf '#!/usr/bin/env bash\nsleep 300\n' > slow_check.sh && chmod +x slow_check.sh
git add -A && git commit -qm baseline && git rev-parse HEAD   # record this sha in the report
# Per-workspace state dir — a pinned field of setup's --json schema (surface/02), not hand-computed:
STATE="$("$PY" -m chinamax setup --json | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["workspace_state_dir"])')"

# `status <id> --wait` is bounded (~240 s) and returns early on log/phase progress — it does NOT
# block to terminal, and `result` refuses an active Job (exit 2). Poll on the EXIT CODES jobs/01
# pins for exactly this purpose (0 = terminal, 2 = still active, 1 = usage/resolution error);
# never text-match, because status embeds uncontrolled log previews in which a tool line saying
# "completed" would end the loop early.
poll() {  # $1 = job id, $2 = evidence file
  local rc
  while true; do
    "$PY" -m chinamax status "$1" --wait | tee -a "$2"; rc=${PIPESTATUS[0]}
    case "$rc" in
      0) return 0 ;;                                     # terminal
      2) continue ;;                                     # still active
      *) echo "poll: status exited $rc — aborting" | tee -a "$2"; return 1 ;;
    esac
  done
}

# --- Runs 1-3 are dispatched from a Claude session rooted at $W, through the Bridge Agent
# (Agent tool, subagent_type chinamax:chinamax) or /chinamax:task — NOT from this shell — so the
# plugin's own state root and workspace key are the ones under test. Capture each relayed job id.

# Run 1 (simple). Prompt: "Create hello.py printing 'hello world', run it, then report."
J1=   # <- paste the job id the Bridge relayed
poll "$J1" "$W/evidence/run1-status.log" || exit 1
"$PY" -m chinamax result "$J1" | tee "$W/evidence/run1-result.txt"
# On-disk artifact is the oracle, not changed_files (ADR 0007) — and assert the OUTPUT, since a
# file that merely exists and exits 0 would otherwise pass.
[ "$("$PY" "$W/hello.py")" = "hello world" ] || { echo "run 1 FAILED: wrong stdout"; exit 1; }

# Run 2 (steer). Prompt names both files: note_one.md, then ./slow_check.sh, then note_two.md.
# The steer is sent BY MESSAGING THE BUSY BRIDGE (that is the surface behavior under test) —
# not via the `steer` CLI verb, which jobs/03 already covers hermetically.
J2=   # <- paste the job id the Bridge relayed
poll "$J2" "$W/evidence/run2-status.log" || exit 1
"$PY" -m chinamax result "$J2" | tee "$W/evidence/run2-result.txt"
# Authoritative steer evidence: the injected user message in the Thread. Match the steer TEXT too,
# so an assistant turn merely echoing the marker cannot be mistaken for the injection.
grep -n '\[steer\].*comment header' "$STATE/jobs/$J2.thread.jsonl" \
  | tee "$W/evidence/run2-steer-transcript.txt"
head -3 "$W"/note_one.md "$W"/note_two.md | tee "$W/evidence/run2-headers.txt"

# Run 3 (70+ min survival). Prompt: 16 numbered edits; "after EVERY item, including the last, run
# ./slow_check.sh in the foreground; wait for it to finish; never background it."  16 x 300 s = 80 min.
J3=   # <- paste the job id the Bridge relayed
# Durable 60 s sampler — replaces `watch` (transient, screen-clearing, dies with the terminal).
# setsid so it survives the deliberate session restart this run exists to test; it breaks itself
# when status reports terminal (exit 0), so it cannot outlive the gauntlet.
setsid nohup env PY="$PY" J="$J3" bash -c \
  'while true; do date -Is; "$PY" -m chinamax status "$J"; \
     "$PY" -m chinamax status "$J" --wait >/dev/null 2>&1; [ $? -eq 0 ] && break; sleep 60; done' \
  >> "$W/evidence/run3-status.log" 2>&1 &
SAMPLER=$!
# Between minutes 25 and 45: exit the Claude session entirely, start a fresh one rooted at $W.
# The SessionStart digest is injected as session CONTEXT, not into this shell — copy it from the
# fresh session into the evidence file by hand; these two commands are the shell-side corroboration.
"$PY" -m chinamax status "$J3"           | tee -a "$W/evidence/run3-restart.txt"
"$PY" -m chinamax logs   "$J3" --tail 20 | tee -a "$W/evidence/run3-restart.txt"
# Terminal assertions — poll FIRST; calling result while the Job is active exits 2.
poll "$J3" "$W/evidence/run3-status.log" || exit 1
kill "$SAMPLER" 2>/dev/null
"$PY" -m chinamax result "$J3" | tee "$W/evidence/run3-result.txt"   # completed, all 16 items
"$PY" -m chinamax logs   "$J3" | tee "$W/evidence/run3-log.txt"
# Executable elapsed assertion — the headline criterion, not a comment. Also refuses `interrupted`,
# which is terminal-but-not-success and must never be scored as a pass.
"$PY" - "$STATE/jobs/$J3.json" <<'PYCHK'
import json, sys, datetime as dt
r = json.load(open(sys.argv[1]))
assert r["status"] == "completed", f'status={r["status"]} (interrupted/failed is not a pass)'
mins = (dt.datetime.fromisoformat(r["completedAt"]) - dt.datetime.fromisoformat(r["startedAt"])).total_seconds()/60
print(f"elapsed {mins:.1f} min")
assert mins > 70, f"{mins:.1f} min < 70 — sleeps were batched or backgrounded; re-run"
PYCHK

# --- Runs 4-7 (smokes, CLI dispatch is fine here — genericity, not surface, is what they prove) ---
for P in kimi glm minimax mimo; do
  J=$("$PY" -m chinamax task --profile "$P" --workspace "$W" \
        "Create $P.txt containing the model's name, then report.")
  poll "$J" "$W/evidence/smoke-$P.log" || { echo "smoke $P FAILED"; continue; }
  "$PY" -m chinamax result "$J" | tee "$W/evidence/smoke-$P-result.txt"
  [ -s "$W/$P.txt" ] || echo "smoke $P: $P.txt missing or empty"
  echo "smoke $P job=$J"     # record id + duration in the report
done

# --- Report: the PLUGIN repo, absolute path (the shell above is cd'd into the throwaway) ---
cd /home/klg2138/deepseek_plugin
git add docs/verification-report.md README.md repo-map.md && git commit
```

## Out of scope

New runtime/jobs features (any gap found live is filed back to its owning scope, not patched here); performance tuning; cost optimization of the gauntlet.
