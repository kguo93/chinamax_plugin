# repo-map — scripts/

The shell shims are shared by both Hosts. `_interpreter.sh` selects the Host
marker and its isolated data root before resolving the Runtime interpreter.

The plugin's thin shell shims. Each resolves the `chinamax` conda interpreter and
`exec`s a python entrypoint; interpreter discovery lives in exactly one place.

- `_interpreter.sh` — sourced (never run) helper carrying THE interpreter-discovery
  order: recorded `<data root>/python-path` → `$CHINAMAX_PYTHON` →
  native macOS/Linux or Windows Miniconda paths → `conda run -n chinamax python`
  → system Python with the plugin's `src/` on `PYTHONPATH` (the bootstrap rung).
  On Windows it normalizes native paths through `cygpath`, uses `;` Python path
  lists, and keeps Git Bash as the shell. Provides `chinamax_data_root`,
  `chinamax_resolve_python`, and `chinamax_exec <module>`.
- `chinamax` — the CLI launcher: `chinamax_exec chinamax "$@"`. Referenced by every
  command file as `"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" <verb> "$ARGUMENTS"`.
- `session_start_hook` — the SessionStart shim: `chinamax_exec
  chinamax.hooks.session_start`. Registered in `hooks/hooks.json`.
- `session_end_hook` — the SessionEnd shim: `chinamax_exec
  chinamax.hooks.session_end`. Registered in `hooks/hooks.json`.
- `stop_hook` — the Stop shim: `chinamax_exec chinamax.hooks.stop`. Registered in
  `hooks/hooks.json`.
- `user_prompt_hook` — the UserPromptSubmit shim: `chinamax_exec
  chinamax.hooks.user_prompt`. Registered in `hooks/hooks.json`.
- `bridge_contract_hook` — the PreToolUse(Bash) shim. Unlike the others it buffers
  stdin and fast-paths: unless the event contains `chinamax:chinamax` it exits 0
  WITHOUT launching python (this fires on every Bash call), then pipes the buffered
  payload into `chinamax_exec chinamax.hooks.bridge_contract`. Registered in
  `hooks/hooks.json`.
- `codex_pretool_hook` — Codex Host-aware pre-tool guard shim for mutating
  Agent/spawn/setup calls outside yolo.
