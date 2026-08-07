"""`commands/task.md`: the arg mapping, the Agent-tool wiring, and the embedded
persistent-Bridge contract it carries.

A named spawn gets a generic system prompt and ignores the agent frontmatter, so
the full contract travels in the task command's spawn `prompt`. These tests pin
that, plus a THREE-WAY lockstep check that the shared stanzas match both
`agents/chinamax.md` and the `bridge_contract` hook's injected `CONTRACT`
constant, so the Bridge contract cannot silently diverge across its three copies.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMAND = (REPO_ROOT / "commands" / "task.md").read_text(encoding="utf-8")
AGENT = (REPO_ROOT / "agents" / "chinamax.md").read_text(encoding="utf-8")
CONTRACT = (REPO_ROOT / "skills" / "chinamax-bridge" / "SKILL.md").read_text(
    encoding="utf-8"
)

#: Stanzas that MUST appear (whitespace-normalized) in ALL THREE Bridge-contract
#: copies — the task command's embedded prompt, the agent contract, and the
#: hook-injected `CONTRACT` — so changing one without the others fails the suite.
SHARED_STANZAS = (
    "never do the task yourself",
    "classify each message from main",
    "exactly one sendmessage(to='main')",
    "no progress messages",
    "unsure between cancel and steer",
    "bridge model",
    "profile model",
    "never put the bridge model into `--model` or send it to the worker.",
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

    # The persistent teammate name convention (never a random suffix).
    assert "chinamax-<profile>-<task-slug>" in text

    # The seam argv it normalizes onto — agreed with the seam, not a second dialect.
    assert "--profile" in text
    assert "--read-only" in text
    assert "--bash-timeout-s" in text
    # The Bridge passes its own name at dispatch so the roster/status stay populated.
    assert "--bridge-name" in text
    # The task text goes on STDIN, never argv.
    assert "stdin" in lower

    # There are NO --resume/--fresh routing controls any more.
    assert "--resume" not in text
    assert "--fresh" not in text

    # The 120 s long-poll and the per-dispatch `poll=` override travel in the
    # embedded contract, with the Bash timeout (180 s) kept above the seam bound.
    assert "--timeout-ms 120000" in text
    assert "poll=" in lower
    assert "180000" in text
    # The old 900 s / 960 s numbers are gone.
    assert "900000" not in text
    assert "960000" not in text


def test_contract_lockstep_three_way():
    """The task adapter and canonical skill share the load-bearing contract."""
    command = _normalized(COMMAND)
    contract = _normalized(CONTRACT)
    for stanza in SHARED_STANZAS:
        assert stanza in command, f"missing from commands/task.md: {stanza!r}"
        assert stanza in contract, f"missing from canonical Bridge skill: {stanza!r}"
    assert "skills/chinamax-bridge/skill.md" in _normalized(AGENT)


def test_profile_model_override_present_in_contract_copies():
    """The Profile model override is distinct from each Host's Bridge model."""
    # All contract copies carry the Profile model mapping plus the Bridge
    # model / Profile model terminology (replaces codex_model/claude_model).
    for text in (COMMAND, AGENT, CONTRACT):
        assert "model=<string>" in text
        assert "--model='<string>'" in text
        assert "Profile" in text
        assert "spaces or quote characters" in _normalized(text)
        assert "Bridge model" in text
        assert "Profile model" in text
