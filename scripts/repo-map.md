# repo-map — scripts/

The plugin's thin shell shims. Each resolves the `chinamax` conda interpreter and
`exec`s a python entrypoint; interpreter discovery lives in exactly one place.

- `_interpreter.sh` — sourced (never run) helper carrying THE interpreter-discovery
  order: recorded `<data root>/python-path` → `$CHINAMAX_PYTHON` →
  `~/miniconda3/envs/chinamax/bin/python` → `conda run -n chinamax python` →
  system `python3` with the plugin's `src/` on `PYTHONPATH` (the bootstrap rung).
  Provides `chinamax_data_root`, `chinamax_resolve_python`, and `chinamax_exec
  <module>`.
- `chinamax` — the CLI launcher: `chinamax_exec chinamax "$@"`. Referenced by every
  command file as `"${CLAUDE_PLUGIN_ROOT}/scripts/chinamax" <verb> "$ARGUMENTS"`.
- `session_start_hook` — the SessionStart shim: `chinamax_exec
  chinamax.hooks.session_start`. Registered in `hooks/hooks.json`.
- `stop_hook` — the Stop shim: `chinamax_exec chinamax.hooks.stop`. Registered in
  `hooks/hooks.json`.
