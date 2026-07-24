"""The result-handling skill (and result.md) carry the duplication-guard language.

The Bridge-contract half of AC bullet 5 is covered by
`test_bridge_contract.py::test_required_stanzas_present`; it is NOT duplicated here.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL = (REPO_ROOT / "skills" / "chinamax-results" / "SKILL.md").read_text(encoding="utf-8")
RESULT_COMMAND = (REPO_ROOT / "commands" / "result.md").read_text(encoding="utf-8")


def test_guard_language_present():
    """The skill carries a trigger-naming description, treat-as-data, and the
    report-and-stop / no-substitute rule; result.md carries report-and-stop too."""
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

    # commands/result.md carries the same report-and-stop rule inline.
    result_lower = RESULT_COMMAND.lower()
    assert "stop" in result_lower
    assert "substitute" in result_lower
    assert "adr 0010" in result_lower


def _frontmatter_field(text: str, field: str) -> str:
    """Return a YAML frontmatter scalar value (single line)."""
    for line in text.splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"frontmatter field {field!r} not found")
