# repo-map — tests/

The pytest suite. Every test drives the Runtime at its process/CLI seam against the hermetic fake provider — never with SDK-level mocks and never against a real endpoint (ADR 0011).

- `fake_provider.py` — the test bed, not a test: `FakeProvider` (a `ThreadingHTTPServer` on an ephemeral port serving `POST /v1/messages` as SSE from a scripted turn list, recording every request's body and headers) plus the script builders `turn()`, `text_block()`, `tool_use_block()`.
- `conftest.py` — shared fixtures and scripts: the autouse `keyless_home` (temp `HOME`, synthetic `model-keys.env`, ambient `ANTHROPIC_*` removed), `start_fake_provider`, and `job_env` — a `JobEnv` factory that binds a Profile to the fake through the real overlay path and runs a Job via `main(["exec", ...])`. Also holds the shared `BASH_COMMAND`/`REPORT_PAYLOAD` constants, `bash_then_report_script()`, the `OMIT` spec sentinel, and the `write_overlay`/`write_keys` helpers.
- `test_fake_provider.py` — the harness itself: a scripted text turn is valid SSE for a raw SDK client.
- `test_walking_skeleton.py` — the loop: bash then report_result, the tool_result returned to the provider, the tool-less-turn nudge (parametrized over three stop reasons), bearer auth with the exact advertised tool list, and report_result sharing a turn with a sibling tool_use.
- `test_transcript.py` — the Thread: replaying the JSONL reconstructs the history, and the outgoing delta is on disk before the request that carries it.
- `test_spec.py` — job-spec validation fails fast (missing field, wrong type, unknown key, non-existent workspace) with no request reaching the provider.
- `test_profiles.py` — the five shipped rows, overlay merge and field-level partial merge, shell-quoted key resolution, and the missing-profile / unknown-profile / missing-key fail-fast paths.
