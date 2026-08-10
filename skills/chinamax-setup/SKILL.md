---
name: chinamax-setup
description: Preview and consent to Codex Host ChinamaX setup changes.
user-invocable: false
---

Refuse under a Claude Host. Setup mutation requires `permission_mode` exactly
`bypassPermissions`; otherwise tell the operator to rerun `codex --yolo`, explain
that yolo disables approval/sandbox enforcement, and identify Runtime
`--read-only` as the worker enforcement boundary.

Run the Codex setup planner first. It must be genuinely non-mutating, show a
redacted content-addressed preview and consent digest, and ask one explicit
yes/no question. Apply only after consent and a matching recomputed digest.
Preserve TOML comments and unrelated config, never copy or print credentials,
never persist yolo, and use the shared deterministic compiler for the managed
`~/.codex/agents/chinamax_bridge.toml`. An unmanaged collision needs a second
overwrite confirmation. A declined agent install warns but does not disable
dynamic Terra/low spawning.

The CLI seam is `CHINAMAX_HOST=codex scripts/chinamax setup --json` for Phase A.
After an affirmative answer, rerun the preview, then use
`CHINAMAX_HOST=codex CODEX_PERMISSION_MODE=bypassPermissions scripts/chinamax setup
--apply --consent-digest <digest> --json`; add `--confirm-overwrite` only after
the separate unmanaged-file confirmation. A stale digest aborts without applying.

On native Windows, run these seams through Git Bash (`shell: bash`) and quote
`$PLUGIN_ROOT/scripts/chinamax`; do not rewrite the command as PowerShell or
CMD. `bash`, `git`, and `cygpath` are prerequisites and setup will report a
missing tool before mutating files.
