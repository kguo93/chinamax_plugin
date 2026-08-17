---
name: chinamax-status
description: Show Codex Host ChinamaX Job status and diagnostics.
user-invocable: false
---

Refuse under a Claude Host. Run the shared Runtime status verb with
`CHINAMAX_HOST=codex`. Status and profiles diagnostics work outside yolo, but
state index and supervision stamps may be blocked by Codex sandboxing; render
the underlying Runtime output and report a visible unsupported-permission
warning rather than failing. The bare listing ends with a `policy:` footer
showing the three worker toggles (memory / hooks / mcp), any malformed-settings
flag, and the full `settings.json` path — render it verbatim. A task mutation
requires `codex --yolo`.
