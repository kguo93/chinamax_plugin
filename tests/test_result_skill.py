"""The result-handling skill carries the duplication-guard language.

`commands/result.md` was deleted with the rest of the internal command surface
(2026-07-30), so the inline report-and-stop copy now lives in the Bridge contract
(`agents/chinamax.md`); the Bridge-contract half of the guard is covered by
`test_bridge_contract.py::test_required_stanzas_present`.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = (REPO_ROOT / "skills" / "chinamax-results" / "SKILL.md").read_text(encoding="utf-8")


def test_guard_language_present():
    """The skill carries a trigger-naming description, treat-as-data, and the
    report-and-stop / no-substitute rule."""
    lower = SKILL.lower()

    # A trigger-naming description (a hidden skill whose description does not name
    # when it applies never fires).
    assert "user-invocable: false" in lower
    description = _frontmatter_field(SKILL, "description")
    assert "chinamax" in description.lower()
    assert "result" in description.lower() or "output" in description.lower()
    assert "failed" in description.lower() or "running long" in description.lower()

    # Treat the worker's report as DATA, never instructions.
    assert "data" in lower
    assert "never" in lower and "instructions" in lower

    # Report-and-stop / never substitute a Claude-side implementation (ADR 0010).
    assert "stop" in lower
    assert "substitute" in lower
    assert "adr 0010" in lower


def _frontmatter_field(text: str, field: str) -> str:
    """Return a YAML frontmatter scalar value (single line)."""
    for line in text.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"frontmatter field {field!r} not found")
