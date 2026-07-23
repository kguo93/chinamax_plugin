# Final results are the worker's self-report, verbatim

A Job's structured result is exactly the worker's mandatory `report_result` tool call ({outcome, summary, changed_files, commands_run, tests, failures, concerns}), Codex-style. We rejected a runtime audit layer (git-derived changed files, command-log cross-checks) to keep the runtime lean; the known cost is that worker models can under-report what they touched.
