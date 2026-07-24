# Final results are the worker's self-report, envelope stripped, prose untouched

A Job's structured result is exactly the worker's mandatory `report_result` tool call ({outcome, summary, changed_files, commands_run, tests, failures, concerns}), Codex-style. We rejected a runtime audit layer (git-derived changed files, command-log cross-checks) to keep the runtime lean; the known cost is that worker models can under-report what they touched.

**Amended 2026-07-24**: the Bridge no longer relays the report verbatim. It strips the report scaffolding — status headers, the `report_result` envelope, "task completed" boilerplate — and fixes layout, so the final response reads as a clean answer rather than a raw worker dump. The worker's own sentences stay untouched: the Bridge may not omit, summarize, verify, judge, or add content of its own. Rejected: free summarization (haiku would drop detail on long Jobs) and full-rewrite-as-own-prose (weakens the fidelity guarantee this ADR exists for).
