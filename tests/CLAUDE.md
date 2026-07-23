# tests/ — conventions

Inventory lives in `./repo-map.md`. Run the suite with the command in the root `CLAUDE.md` — it needs the `chinamax` conda env and the editable install.

## Gotchas

- **The suite is keyless and endpoint-clean by construction.** The autouse `keyless_home` fixture points `HOME` at a temp dir and deletes the ambient `ANTHROPIC_*` variables, so tests added later inherit the guarantee instead of opting in. It only holds because the Runtime resolves `~/.claude/...` through `Path.home()`. `test_bearer_auth_and_advertised_tools` re-sets `ANTHROPIC_API_KEY` on purpose — that seeding IS the test; without it the assertion passes even on an implementation that never sanitizes the environment.
- **Bind the endpoint through the Profile overlay, never an env var.** `job_env` writes `~/.claude/chinamax-profiles.json` pointing the Profile's `base_url` at the fake. There is deliberately no endpoint backdoor: the overlay is the seam, which keeps the tests exercising the real resolution path.
- **Never assert inside a fake-provider handler.** `http.server` swallows handler exceptions to stderr, so an in-handler `assert` is silently vacuous. Record the observation on the request (as `transcript_snapshot` does) and assert on it in the main thread.
- **The fake provider is function-scoped and torn down deterministically:** `shutdown()`, then `server_close()`, then join. `shutdown()` alone only stops the serve loop; `server_close()` is what releases the listening socket. It stays on HTTP/1.0 so no keep-alive handler thread can outlive the test.
- **A request past the end of the script returns a marked 500, never a hang.** The loop has no turn cap by design and the suite sets no global timeout, so a termination bug must fail in one turn instead of spinning forever. If a test hangs, suspect the script ran short.
- **Compare results parsed, never as bytes.** The SDK reparses `input_json_delta` into a dict, so a byte comparison asserts on serialization formatting rather than on payload fidelity.
- **Assert external behavior at the seam** — files produced, transcript written, result stored, requests recorded — never internal call sequences. `JobEnv.observations()` reads the tool_results back out of the durable Thread, which is the seam for anything a tool reported.
- **Script one tool call per turn** (`tool_script`, `bash_script`). A test that asserts the loop *continued* after a failed tool needs a later turn to have executed; packing calls into one turn cannot show that.
- **A negative assertion must not be satisfiable by the message itself.** `grep` echoes its pattern back in "no matches for 'X'", so asserting `"X" not in observation` passes vacuously — `test_recursive_symlink_not_followed` uses a different token for the pattern and for the outside file's contents on purpose.
- **`JobEnv.tree()` walks with `followlinks=False`**, not `rglob`, for the same reason the Runtime does: on Python 3.12 `rglob` follows directory symlinks and a confinement test would walk out of its own workspace.
- **The process-group test is only meaningful because an orphan normally survives.** `test_timeout_observation_continues` asserts a backgrounded descendant is dead; killing the child alone leaves it running, which is what makes the assertion bite.
