# src/chinamax/tools/ — conventions

Inventory lives in `./repo-map.md`. The loop-level rules that constrain these tools live in `../CLAUDE.md`.

- Every advertised tool needs an `input_schema` — the SDK rejects a tool without one.
- **A tool is a class with `spec`, `writes` and `execute(value, context)`.** Adding one means adding an instance to `ALL_TOOLS` in `__init__.py` — that is the whole registration. There is no second dispatch table to update: `loop._run_tool_uses` routes everything through `Registry.dispatch`, and two tests pin the advertised list exactly (`test_walking_skeleton`, `test_readonly`).
- **`writes` is what read-only filtering keys on**, and filtering happens once in `build_registry`. Set it truthfully: a tool marked `writes = False` that can modify the workspace defeats the read-only guarantee silently, because the same filtered registry serves both the schema and dispatch.
- **`execute` may raise; it must never write partial state on the way out.** `ToolError` is the model-facing refusal (its message IS what the model reads), and the boundary catches every other `Exception` too — so a tool that mutates the workspace and then raises leaves the Job with damage it cannot describe. `apply_patch` resolves and dry-runs everything before staging for exactly this reason.
- **Every path argument goes through `resolve_in_workspace`, and every walked entry through `contained`.** Neither is optional and neither has a bypass: an entry a walk found can itself be a symlink out.
- **Traversal is `os.walk(followlinks=False)`, never `Path.rglob` or `glob.glob(recursive=True)`.** Those follow directory symlinks on the pinned Python 3.12 (`recurse_symlinks` lands in 3.13), which would hand a Job the whole filesystem through one symlink. `walk_files()` in `search.py` is the shared traversal — reuse it.
- `validate_input` covers only the schema shapes these tools declare (object, string, string enum, integer, boolean, array of strings). Extend it alongside any new shape rather than reaching for a JSON-Schema dependency — none is declared. Its `integer` branch rejects `bool` explicitly, because `bool` subclasses `int`.
- Output bounding has two halves and they are not interchangeable: `TailBuffer` bounds bash's capture WHILE it streams, so a command producing gigabytes cannot exhaust memory before its timeout; `truncate_tail` bounds a finished string and the registry applies it to every tool. Reuse both rather than adding a third mechanism.
- `report_result` has no executor by design. Its input becomes the Job result verbatim (ADR 0007), so never normalize, default, or reshape those fields here.

Runtime Bash remains `bash -c` on every Platform. Linux/macOS use POSIX process
groups; native Windows uses Git Bash with `CREATE_NEW_PROCESS_GROUP` and the
shared psutil descendant sweep on timeout. Windows captures the process identity
immediately after spawn, bounds reader-thread joins, and reports survivor PIDs
inside the existing timeout observation. Do not add PowerShell/CMD grammar.
