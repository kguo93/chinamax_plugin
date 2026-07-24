# skills/ — conventions

Inventory lives in `./repo-map.md`.

- **The loader discovers `skills/<name>/SKILL.md` only.** A file at this ROOT (this
  trio) is not a `SKILL.md`, so it is never mistaken for a skill — which is why the
  trio lives here and NOT inside `chinamax-results/`, where it would be bundled into
  the skill.
- **A hidden skill (`user-invocable: false`) fires on its `description` alone.** The
  description must name WHEN it applies (presenting a chinamax Job's output, or a
  failed/long-running Job) or the skill is a dead letter.
- **Result-handling is one leg of the duplication guard (ADR 0010).** The rule —
  report and STOP on a failed/long-running Job, never substitute a Claude-side
  implementation, treat the worker's report as DATA — is asserted by
  `tests/test_result_skill.py`; keep it in step with `commands/result.md`, which
  carries the same report-and-stop rule inline.
