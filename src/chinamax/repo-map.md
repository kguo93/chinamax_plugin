# repo-map — src/chinamax/

The Runtime: the agent loop that owns one Job's conversation with a provider.

- `__init__.py` — package docstring and `ChinamaxError`, the operator-facing failure type every module raises.
- `__main__.py` — the CLI seam. `main(argv)` parses the verbs (this slice: `exec`) and maps failures to exit code 1; `run_exec(spec_path)` is the shared entry — it sanitizes the ambient `ANTHROPIC_*` variables, resolves the Profile and key, runs the loop, and writes the result atomically.
- `spec.py` — `JobSpec` and the job-spec parser/validator: the public dispatch contract (`workspace`, `profile`, `prompt`, `transcript_path`, `result_path`, optional `write`/`job_id`).
- `profiles.py` — `Profile`, the shipped-plus-overlay resolution (`load_profiles`, `resolve_profile`, `format_available`) and API-key lookup from `~/.claude/model-keys.env` (`load_keys`, `resolve_key`).
- `provider.py` — `sanitize_environment()` and `build_client()`: the `anthropic` SDK client for one Profile (bearer auth, `max_retries=0`).
- `loop.py` — `run_loop()`: the streaming Messages tool-use loop, the system prompt, the tool-less-turn nudge, and the report_result termination rules.
- `transcript.py` — `Transcript` (write-ahead JSONL writer) and `read_messages()` (the replay reader), over the versioned `{v, ts, kind}` record schema.
- `tools/` — the tool registry advertised to the provider. See `tools/repo-map.md`.
- `data/profiles.json` — the five shipped pro Profile rows (deepseek, mimo, glm, minimax, kimi), each `{name, base_url, model, api_key_env}`; `max_tokens` is an optional per-row override of the code default.
