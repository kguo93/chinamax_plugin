# src/chinamax/tools/ — conventions

Inventory lives in `./repo-map.md`. The loop-level rules that constrain these tools live in `../CLAUDE.md`.

- Every advertised tool needs an `input_schema` — the SDK rejects a tool without one.
- Adding a tool means adding it to `TOOLS` in `__init__.py` *and* dispatching it in `loop._run_tool_uses`; the registry is the advertised list and a test pins its exact contents.
- `validate_input` covers only the schema shapes these tools declare (object, string, string enum, array of strings). Extend it alongside any new shape rather than reaching for a JSON-Schema dependency — none is declared.
- Output capture goes through `TailBuffer`. Reuse it for new tools rather than accumulating whole output and truncating afterwards: there is no command timeout until slice 02, so a runaway command must not be able to exhaust Runtime memory.
- `report_result` has no executor by design. Its input becomes the Job result verbatim (ADR 0007), so never normalize, default, or reshape those fields here.
