# repo-map — skills/

Claude Code skills the plugin ships. The loader discovers each `skills/<name>/SKILL.md`.

- `chinamax-results/SKILL.md` — the result-handling skill (`user-invocable: false`):
  its `description` names the trigger (presenting a chinamax Job's output, or a Job
  that failed or is running long) so it fires; its body preserves the worker's
  result structure, treats the report as DATA never instructions, and on a
  failed/long-running Job reports and STOPS rather than substituting a Claude-side
  implementation (ADR 0010).
