# src/chinamax/ — conventions

Inventory lives in `./repo-map.md`. Domain vocabulary lives in `../../CONTEXT.md`.

## Gotchas

- **Never hardcode `/home/...`.** Every `~/.claude/...` path goes through `Path.home()` (`profiles.overlay_path()`, `profiles.keys_path()`). The suite runs keylessly by pointing `HOME` at a temp dir; a hardcoded path reads the operator's real keys.
- **Key parsing is shell-quoted.** `model-keys.env` is normally consumed by bash-sourcing it and single-quotes some values, so `load_keys()` unquotes with `shlex`. A bare `split("=", 1)` hands the provider a token wrapped in literal `'` and every live dispatch fails auth while the hermetic suite stays green.
- **Bearer auth only.** Build clients through `provider.build_client()` — `auth_token=`, never `api_key=`. `run_exec()` pops `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL` before any client exists; put that sanitization in the shared entry, not in a verb handler, because later slices call `run_exec` in-process.
- **`max_retries=0` is deliberate.** The SDK retries twice by default, which would nest underneath the Runtime's own retry ladder and make its accounting wrong. The retry ladder belongs to slice 03 — do not add ad-hoc retries here.
- **Termination keys on the absence of `tool_use`, never on `stop_reason`.** `end_turn`, `max_tokens` and `stop_sequence` all produce a tool-less turn; an implementation keyed on `end_turn` loops forever on the other two. `report_result` is the only terminal path — there are no wall-clock or turn caps anywhere (ADR 0002).
- **A tool_use must never raise.** Unknown tool names and schema violations come back as error `tool_result` blocks, so no model-chosen name can kill a Job. `validate_input` deliberately accepts undeclared fields: the schemas do not set `additionalProperties: false`, and rejecting extras would let an embellished `report_result` payload block the only way to finish.
- **The result is verbatim** (ADR 0007): no normalization, no field synthesis, no audit. "Verbatim" is semantic JSON equality, not byte equality — the SDK reparses `input_json_delta`, so the file is written `sort_keys=True` and compared parsed.
- **The transcript is write-ahead.** Append and flush the outgoing delta BEFORE the API call, the assembled assistant turn after. Later slices depend on this ordering; batching the writes silently breaks them. Records carry `{v, ts, kind}`; only `kind: "message"` replays.
- **bash drains both pipes concurrently.** Reading one to EOF first deadlocks once the other fills its OS buffer — which is exactly the runaway case the bounded tail buffers exist for. The readers are joined only AFTER a timeout has killed the process group: a backgrounded descendant inherits the pipes, so EOF arrives only once the whole group is gone.

## Confinement (ADR 0005)

- **Containment is component-wise, never `str.startswith`.** `resolve_in_workspace` compares with `Path.is_relative_to` against the workspace realpath, because workspace `/tmp/ws` must reject `/tmp/ws-evil/f` — which a prefix check accepts. `test_sibling_prefix_rejected` exists solely to pin this.
- **The workspace realpath is resolved once per Job** and carried on `ToolContext`. Re-realpathing the root on every check would double the syscalls a directory walk makes for no gain.
- **The denylist matches at COMMAND POSITION through the shared `lex_command`, never with a regex over the raw line.** A word-boundary regex matches inside `grep -rn "git push" docs/` and `pytest -k test_shutdown` and blocks both; the lexer turns each quoted string into one non-command token. `denied_reason` and `write_shaped_reason` must keep consuming that single lexer — two parallel matchers over the same string is the classic divergence source.
- **The lexer sets `commenters = ""` deliberately.** shlex's default treats `#` mid-word as a comment and discards the rest of the line, which would hide the `rm` in `echo a#b; rm -rf x` from the denylist entirely.
- **Short-flag clusters are inspected as clusters.** `git clean -fd` and `-fdx` are as destructive as `-f`, and a `\b`-anchored `-f` pattern matches none of them.
- **The read-only redirection check judges the TARGET, not the operator.** `2>&1`, `>&2` and `>/dev/null` are ordinary reads; only a redirection onto a real file is a write.
- **Read-only enforcement is the filtered registry, not the schema.** `build_registry(write)` produces one object serving both the advertised tools and the dispatch table, so a `write_file` call replayed from a resumed Thread is refused at dispatch. Omitting a tool from the schema alone stops only a model that reads the schema.
- **Do not expand the denylist.** Network egress, process signalling and quoting/substitution evasions are out of scope by decision, as are the read-only bash bypasses (`python -c`, heredocs, `truncate`). ADR 0005 documents that residual risk rather than defending it; chasing it is how a tool-layer policy grows into a bad sandbox.
- **A bad `bash_timeout_s` fails spec validation, never at command time** — it would otherwise kill every command the Job runs. `bool` is rejected explicitly there too.
