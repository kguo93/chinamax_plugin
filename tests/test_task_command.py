"""`commands/task.md`: the arg mapping and the Agent-tool wiring it documents."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMAND = (REPO_ROOT / "commands" / "task.md").read_text(encoding="utf-8")


def test_command_arg_mapping():
    """The command normalizes the pinned seam argv and wires up the Agent tool."""
    text = COMMAND
    lower = text.lower()

    # Frontmatter lists Agent in allowed-tools (what keeps the tool in scope).
    assert "allowed-tools: Agent" in text

    # Invokes the Bridge by subagent type, INLINE (a forked general-purpose
    # subagent does not expose the Agent tool), as a BACKGROUND addressable agent
    # (a foreground subagent could not receive a mid-run Steer).
    assert "chinamax:chinamax" in text
    assert "inline" in lower
    assert "background" in lower

    # The seam argv it normalizes onto — agreed with jobs/01, not a second dialect.
    assert "--profile" in text
    assert "--read-only" in text
    assert "--bash-timeout-s" in text
    # The task text goes on STDIN, never argv.
    assert "stdin" in lower

    # `--resume`/`--fresh` are Bridge-level routing controls, not task flags.
    assert "--resume" in text
    assert "--fresh" in text
