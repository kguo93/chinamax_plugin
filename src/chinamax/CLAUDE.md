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
- **`spec.write` is advisory here.** It shapes the system prompt only; a `write: false` Job can still write through bash until slice 02 lands read-only enforcement. Do not build that enforcement early.
- **bash drains both pipes concurrently.** Reading one to EOF first deadlocks once the other fills its OS buffer — which is exactly the runaway case the bounded tail buffers exist for.
