# Official anthropic SDK in a dedicated conda env

**Amended 2026-08-06.** The same `chinamax` environment is used by both Host
adapters; `tomlkit` is added for lossless Codex TOML setup edits.

The Runtime is Python 3.12 in a fresh conda env `chinamax` (never the shared py_automation env) and speaks to every Profile through the official `anthropic` SDK pointed at the profile's base URL — native streaming and tool-use blocks instead of ~300 lines of hand-rolled wire/SSE code. Compatibility is considered proven because the same endpoints already serve Claude Code itself in the implement-handoff skill.

**Amended 2026-08-01**: the `anthropic` floor in `pyproject.toml` rises `>=0.37` → `>=0.118`. The 2026-08-01 reasoning round depends on the `thinking` kwarg on `Messages.stream` and the `thinking_delta`/`signature_delta` stream accumulation, both absent at 0.37 and verified at 0.118.0; without the bump an environment whose already-installed older SDK satisfies `>=0.37` passes setup and then fails at dispatch. The env/SDK decision itself is unchanged. Cross-reference ADR 0001's 2026-08-01 reasoning amendment.
