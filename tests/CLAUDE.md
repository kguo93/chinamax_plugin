# tests/ — conventions

Inventory lives in `./repo-map.md`. Run the suite with the command in the root `CLAUDE.md` — it needs the `chinamax` conda env and the editable install.

Host migration tests must cover explicit/env/evidence precedence, disjoint roots,
host-tagged records, Codex yolo/name invariants, native-agent compilation, and
manifest parity. Direct CLI tests must provide `--host claude` or Codex evidence;
the no-marker failure is intentional.

Platform migration tests must use injected/mocked macOS and Windows seams on the
Linux runner: native process, lock, path, prerequisite, and hook behavior is
covered without pretending to be live native-OS evidence. Keep the full Linux
suite green and assert that Linux runtime dependency, process, and import behavior
remains unchanged. Linux SETUP is the one documented exception (0.4.5): it now
gates on the `bash`+`miniconda` prerequisites and its report gains
`prerequisites`/`prerequisite_fixes`, so a setup test asserts that new report, not
"report behavior unchanged".

## Gotchas

- **The suite is keyless and endpoint-clean by construction.** The autouse `keyless_home` fixture points `HOME` at a temp dir and deletes the ambient `ANTHROPIC_*` variables, so tests added later inherit the guarantee instead of opting in. It only holds because the Runtime resolves `~/.claude/...` through `Path.home()`. `test_bearer_auth_and_advertised_tools` re-sets `ANTHROPIC_API_KEY` on purpose — that seeding IS the test; without it the assertion passes even on an implementation that never sanitizes the environment.
- **Bind the endpoint through the Profile overlay, never an env var.** `job_env` writes `~/.claude/chinamax-profiles.json` pointing the Profile's `base_url` at the fake. There is deliberately no endpoint backdoor: the overlay is the seam, which keeps the tests exercising the real resolution path.
- **Never assert inside a fake-provider handler.** `http.server` swallows handler exceptions to stderr, so an in-handler `assert` is silently vacuous. Record the observation on the request (as `transcript_snapshot` does) and assert on it in the main thread.
- **The fake provider is function-scoped and torn down deterministically:** `shutdown()`, then `server_close()`, then join. `shutdown()` alone only stops the serve loop; `server_close()` is what releases the listening socket. It stays on HTTP/1.0 so no keep-alive handler thread can outlive the test.
- **A request past the end of the script returns a marked 500, never a hang.** The loop has no turn cap by design and the suite sets no global timeout, so a termination bug must fail in one turn instead of spinning forever. If a test hangs, suspect the script ran short.
- **Compare results parsed, never as bytes.** The SDK reparses `input_json_delta` into a dict, so a byte comparison asserts on serialization formatting rather than on payload fidelity.
- **Assert external behavior at the seam** — files produced, transcript written, result stored, requests recorded — never internal call sequences. `JobEnv.observations()` reads the tool_results back out of the durable Thread, which is the seam for anything a tool reported.
- **Script one tool call per turn** (`tool_script`, `bash_script`). A test that asserts the loop *continued* after a failed tool needs a later turn to have executed; packing calls into one turn cannot show that.
- **Keep read-only coverage on common write shapes.** `test_readonly.py` checks both redirection and command-form writers such as `touch`; the sentinel must remain absent after every refused tool call.
- **Adapter posture is part of the contract.** `test_codex_adapter.py` pins that the Codex task skill forwards an explicit `--read-only` request instead of letting yolo silently turn it into a write-capable Job.
- **A negative assertion must not be satisfiable by the message itself.** `grep` echoes its pattern back in "no matches for 'X'", so asserting `"X" not in observation` passes vacuously — `test_recursive_symlink_not_followed` uses a different token for the pattern and for the outside file's contents on purpose.
- **`JobEnv.tree()` walks with `followlinks=False`**, not `rglob`, for the same reason the Runtime does: on Python 3.12 `rglob` follows directory symlinks and a confinement test would walk out of its own workspace.
- **The process-group test is only meaningful because an orphan normally survives.** `test_timeout_observation_continues` asserts a backgrounded descendant is dead; killing the child alone leaves it running, which is what makes the assertion bite.

## Dispatch tests

- **Real detached processes, never a mocked process layer** (the jobs PRD's testing decisions). `dispatch_env` points `CLAUDE_PLUGIN_DATA` at a temp dir so the real `task` verb spawns a real worker into a temp state root; the operator's own state dir is never touched. `test_spawn_failure_marks_failed` makes `Popen` raise for real by pointing `CHINAMAX_WORKER_PYTHON` at a non-executable file — that override exists FOR that test.
- **`reap()` runs before the fake provider is torn down,** by fixture ordering, so a still-running worker never spends its retry ladder against a dead endpoint and stalls the suite. It waits every worker out first and only then SIGKILLs, and it guards the kill on the recorded `pidStartTime` still matching the live process, so teardown can never signal an unrelated process that inherited the pid. A new dispatch-spawning test must use `dispatch_env`, or its worker leaks past the run.
- **Never SIGKILL a group without checking it is not pytest's own.** `kill_group` asserts `os.getpgid(shell.pid) != os.getpgid(0)` first. The background-execution test is only meaningful because the worker is in its OWN session and therefore outside the killed group — kill the dispatcher alone and the assertion proves nothing.
- **Wait on the LOG, not on the record's phase, to observe a running Job.** Record writes are throttled to one per poll interval; the log is flushed per line and is the channel that is never behind.
- **A `--wait` test that must prove it woke on the LOG has to hold status and phase still,** which a live worker will not do on cue — `running_job()` builds the record directly for exactly that reason. Conversely, `test_wait_returns_early` needs a real worker, because the thing under test is the lag between completion and return.
- **Assert the index CONTENTS, not that `state.json` parses.** A lost index update leaves perfectly valid JSON that is simply missing a Job, which a parse-only assertion passes.
- **`test_fast_worker_does_not_lose_pid_race` pins only the MOMENT the dispatcher reaches its bookkeeping write** — the worker, the store and the compare-and-swap are all genuine. Without that barrier the ordering the test exists to cover is a coin flip.
- **A `logs`/preview escaping test must assert the line COUNT.** The point is that one event is exactly one line; asserting only that `\\x1b` appears passes against an implementation that also emitted the forged second line.

## Lifecycle tests

- **Age the heartbeat, never sleep the grace out.** `aged()` writes an `updatedAt` in the past through the store's own updater with `touch=False`; a test that slept 60 s to make a Job stale would add a minute to every run for nothing.
- **A record the test builds is the only way to observe several of these states.** `build_record()` publishes a `queued`/`running`/finished record with a chosen pid and timestamps, because a live worker will not hold still on a dead pid, a stale heartbeat, or two simultaneously-active Jobs. Every write still goes through the real locked compare-and-swap.
- **Kill a worker only once it is INSIDE the API call.** The transcript is write-ahead, so a SIGKILL fired the moment the record turns `running` lands before the outgoing user turn is flushed and leaves an EMPTY Thread — which resume then refuses, failing the test for the wrong reason. Wait on the fake provider having received the request.
- **A SIGKILLed worker is a ZOMBIE here, not in production.** pytest is its parent and never reaps it, so `worker_gone` sees state `Z` rather than ESRCH — which is worth asserting, and then clearing with `os.waitpid` so the `reap()` teardown does not spend 45 s waiting on a corpse it believes is live.
- **`test_cancel_kills_tree` is only meaningful because of the OWN-SESSION child.** `own_session_children()` asserts the descendant's pgid differs from the worker's before the cancel; a child sharing the worker's group would pass under a single `killpg` and let the real defect through. Zombies count as dead in the after-check — the worker is pytest's child.
- **A resume test needs a SECOND provider.** The source Job exhausted the first script, and a request past the end returns the marked 500, so `env.bind()` a fresh script before resuming or the continued Job fails instead of continuing.
- **Waiting on a pruned COUNT alone passes at the wrong prune.** Dispatch prunes and so does the worker's terminal transition; the count hits 50 after the first one, before the new Job has finished, so the wait must also require the fresh id to be present.
- **Assert pruned artifacts BOTH ways** — the six named paths (so the layout list is pinned) and `job_leftovers()`'s name-prefix sweep (so a seventh artifact added later fails loudly instead of leaking orphans).

## Steer tests

- **The turn gate holds the WORKER, not the test.** `FakeProvider.gate(index)` blocks the handler serving that turn after it records the request; a test waits on `reached`, writes a steer into the between-turns window, then sets `release`. The worker is a detached process but the provider (and thus the gate's events) live in the pytest process, so the test drives both sides. Always `release.set()` — an un-released gate makes `reap()` spend its 45 s grace before SIGKILL.
- **Gate turn 0 to land a steer on the NEXT request; gate the FINAL turn to exercise the undelivered sweep.** A steer written while turn N is held drains at the top of iteration N+1, so it lands in request N+1 — except when N is the terminal turn, whose drain already ran, which is exactly the accept-versus-terminate race the sweep logs.
- **The crash-mid-drain test needs a REAL subprocess** — `STEER_ABORT_VARIABLE` hard-exits via `os._exit`, which would kill pytest in-process. Run 1 is `subprocess.run` with the abort env (exit 137, zero API calls); run 2 is in-process `main(["task-worker", ...])` WITHOUT the abort env (the skipped steer never reaches the abort point anyway). The assertion that bites is ONE delivery across both runs, guarding the zero-delivery seeding bug, not merely "it completed".
- **Reclaim needs a confirmed-dead worker, and `build_record(status=running, pid=dead_pid())` is it.** `JobStore.claim` reclaims on `worker_gone` WITHOUT the stale grace, so a fresh `updatedAt` is fine — do not age it. Relaunch with `task-worker --job-id`, never `resume`: resume builds a new Job with its own empty queue and would never see the file.
- **Pin two same-millisecond writers with `STEER_CLOCK_VARIABLE` across two subprocesses**, not two in-process calls — the point is two seq-0 writers whose names differ only by the random suffix, which one process (its seq counter incrementing) cannot produce.
- **Assert the steer's wire shape with `assert_wire_shape`** (no consecutive same-role, every `tool_use` answered): a steer recorded as its own user record must coalesce back on any re-seed, and the injected turn must carry the tool_result AND the steer as one user message.

## Liveness tests

- **Inject the supervision seams through `run()`, not by monkeypatching.** `JobEnv.run(spec, config=loop_config(sleeper))` hands a `LoopConfig` to the real `run_exec` entry, so backoff is deterministic (identity jitter) and instantaneous (the sleeper only records) while the test still drives the production path. Numbers like `inactivity_timeout_s` go through the JOB SPEC, because that is how a dispatch overrides them in production.
- **A scripted hang gives up on its own after `HANG_BOUND_S`,** so "the Job eventually retried and completed" is NOT evidence the watchdog works — it passes just as well against a watchdog that never breaks the read. `test_midstream_hang_retried` therefore bounds elapsed time too; without that assertion, deleting the socket shutdown leaves the whole suite green and every real hang eternal.
- **`failure_kind` is what separates the fault modes.** A ping-drip that ends at its bound looks like a premature EOF, and a hang whose handler dies looks like a dropped connection — all three are transient and all three retry. Assert on `failure_kind`, never just on "it retried once".
- **Read the reporter's events by shape, not by line number.** `reporter_events()` keeps only lines that parse as JSON objects carrying `event`, because prose errors and any stdlib traceback share stderr with the structured lines.
- **Exhaustion tests script exactly as many faults as the ladder has attempts.** A seventh request would get the marked 500 — itself transient — so the count assertion, not the outcome, is what proves no hidden retry layer.
- **A negative control run against a COPY of the source proves nothing.** `chinamax` is installed editable through `__editable__.chinamax-0.1.0.pth`, which pins imports at `/home/klg2138/chinamax_plugin/src/chinamax` no matter what directory pytest runs from. Mutate a scratch copy, run the suite against it, and every mutation silently no-ops while the control reports green. Force `PYTHONPATH` at the copy's `src/` and assert `chinamax.liveness.__file__` actually points inside the copy before believing any negative-control result. The alternative — mutate in place, run, restore from a backup — is what this slice used, and it is the safer default.
