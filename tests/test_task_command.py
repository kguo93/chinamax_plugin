"""`commands/task.md`: the arg mapping, the Agent-tool wiring, and the embedded
Bridge contract it carries.

A named spawn gets a generic system prompt and ignores the agent frontmatter, so
the full contract travels in the task command's spawn `prompt`. These tests pin
that, plus a lockstep check that the shared stanzas match `agents/chinamax.md` so
the two Bridge contracts cannot silently diverge.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMAND = (REPO_ROOT / "commands" / "task.md").read_text(encoding="utf-8")
CONTRACT = (REPO_ROOT / "agents" / "chinamax.md").read_text(encoding="utf-8")

#: Stanzas that MUST appear (whitespace-normalized) in BOTH the agent contract
#: and the task command's embedded prompt block, so changing one without the
#: other fails the suite.
SHARED_STANZAS = (
    "forbidden to spawn any subagent",
    "relay errors only",
    "strip the report scaffolding",
)


def _normalized(text: str) -> str:
    """Whitespace-collapsed, lower-cased text, so a stanza wrapped across lines
    still matches on prose presence rather than on layout."""
    return re.sub(r"\s+", " ", text).lower()


def test_command_arg_mapping():
    """The command normalizes the pinned seam argv and wires up the Agent tool."""
    text = COMMAND
    lower = text.lower()

    # Frontmatter lists Agent in allowed-tools (what keeps the tool in scope).
    assert "allowed-tools: Agent" in text

    # Invokes the Bridge by subagent type, INLINE (a forked general-purpose
    # subagent does not expose the Agent tool), as a BACKGROUND addressable agent
    # (a foreground subagent could not receive a mid-run Steer), with the cheap
    # model named EXPLICITLY (a named spawn ignores the agent frontmatter).
    assert "chinamax:chinamax" in text
    assert "inline" in lower
    assert "background" in lower
    assert 'model: "haiku"' in text

    # The seam argv it normalizes onto — agreed with jobs/01, not a second dialect.
    assert "--profile" in text
    assert "--read-only" in text
    assert "--bash-timeout-s" in text
    # The task text goes on STDIN, never argv.
    assert "stdin" in lower

    # `--resume`/`--fresh` are Bridge-level routing controls, not task flags.
    assert "--resume" in text
    assert "--fresh" in text

    # The 900 s long-poll and the per-dispatch `poll=` override travel in the
    # embedded contract, with the Bash timeout kept above the seam bound.
    assert "--timeout-ms 900000" in text
    assert "poll=" in lower
    assert "960000" in text


def test_contract_lockstep_with_agent():
    """Each shared stanza is present in BOTH the task prompt and the agent
    contract, so the two Bridge contracts cannot silently drift apart."""
    command = _normalized(COMMAND)
    contract = _normalized(CONTRACT)
    for stanza in SHARED_STANZAS:
        assert stanza in command, f"missing from commands/task.md: {stanza!r}"
        assert stanza in contract, f"missing from agents/chinamax.md: {stanza!r}"
