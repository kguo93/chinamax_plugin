# repo-map — src/chinamax/tools/

The tool registry the Runtime advertises to the provider. This slice ships two tools; the rich set (read_file, write_file, str_replace_edit, list_dir, grep, glob, apply_patch) arrives in slice 02.

- `__init__.py` — the registry itself: `TOOLS` (the advertised list, in order), `TOOLS_BY_NAME`, the `BASH`/`REPORT_RESULT` name constants, and `validate_input()`, the shared `input_schema` checker every tool_use passes through.
- `bash.py` — `BASH_TOOL` (schema), `run_bash()` (cwd-pinned `bash -c` with both pipes drained concurrently), `format_bash_result()` (the tool_result rendering), and `TailBuffer`, the bounded ~50 KB tail capture the later tools reuse.
- `report_result.py` — `REPORT_RESULT_TOOL`: the seven-field completion schema (`outcome` enum and `summary` required) and the `REPORT_RESULT` name constant. The tool has no executor — it is the loop's terminus.
