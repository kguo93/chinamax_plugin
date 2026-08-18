# repo-map — skills/

Claude Code and Codex skills the plugin ships. The loader discovers each `skills/<name>/SKILL.md`.

- `chinamax-bridge/SKILL.md` — canonical Host-neutral Bridge contract (`user-invocable: false`).
- `chinamax-task/SKILL.md` — Codex yolo-only task adapter with underscore-safe
  naming, `--read-only` forwarding, and an explicit trusted-status-to-Runtime
  permission-mode transport for CLI builds that do not export
  `CODEX_PERMISSION_MODE`.
- `chinamax-status/SKILL.md` — Codex diagnostic/status adapter.
- `chinamax-profiles/SKILL.md` — Codex diagnostic Profile adapter.
- `chinamax-setup/SKILL.md` — Codex preview/consent setup adapter.

- `chinamax-results/SKILL.md` — the result-handling skill (`user-invocable: false`):
  its `description` names the trigger (presenting a chinamax Job's output, or a Job
  that failed or is running long) so it fires; its body presents the worker's result
  with the `report_result` envelope stripped and the prose left untouched (amended
  ADR 0007), treats the report as DATA never instructions, and on a failed/long-
  running Job reports and STOPS rather than substituting a Claude-side implementation
  (ADR 0010).

All Codex seam instructions that run on native Windows select Git Bash and quote
the plugin root; the Runtime command grammar is not translated to PowerShell or
CMD.
