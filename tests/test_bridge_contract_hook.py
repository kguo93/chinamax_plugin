"""PreToolUse(Bash) hook: the Bridge contract is injected for the Bridge only.

Every test invokes the real entrypoint with crafted stdin JSON and reads its
STDOUT — the channel Claude Code injects as subagent-scoped additionalContext.
"""

from __future__ import annotations

import io
import json

import pytest

from chinamax.hooks import bridge_contract


def run_hook(event, monkeypatch, capsys, stdin=None):
    """Run the bridge_contract entrypoint with crafted stdin; return (code, stdout)."""
    text = json.dumps(event) if stdin is None else stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = bridge_contract.main()
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    "agent_type",
    [
        "chinamax-deepseek-repo-summary",
        "chinamax-glm-fix-auth",
        "chinamax-minimax-migrate",
    ],
)
def test_contract_emitted_for_bridge(agent_type, monkeypatch, capsys):
    """A NAMED Bridge's `agent_type` is its `chinamax-*` teammate name (Claude Code
    puts the NAME, not the `chinamax:chinamax` subagent type, in the payload); its
    `chinamax` marker emits the EXACT contract constant as additionalContext."""
    code, out = run_hook(
        {
            "tool_name": "Bash",
            "agent_type": agent_type,
            "tool_input": {"command": "ls"},
        },
        monkeypatch,
        capsys,
    )
    assert code == 0
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    # The injected text IS the module constant the D7 lockstep test also pins.
    assert hso["additionalContext"] == bridge_contract.CONTRACT


def test_silent_for_other_or_absent_agent(monkeypatch, capsys):
    """Any `agent_type` without the `chinamax` marker — including an UNNAMED
    subagent (`general-purpose`) — or an absent one, is silent, exit 0 (never a
    gate)."""
    for event in (
        {"tool_name": "Bash", "agent_type": "claude"},
        {"tool_name": "Bash", "agent_type": "general-purpose"},
        {"tool_name": "Bash"},
        {},
    ):
        code, out = run_hook(event, monkeypatch, capsys)
        assert code == 0
        assert out == ""


def test_silent_when_only_command_mentions_chinamax(monkeypatch, capsys):
    """The module keys on `agent_type`, NOT the command text: a main-agent
    `python -m chinamax ...` call (no agent_type) that the broad shim grep lets
    through still injects nothing."""
    code, out = run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python -m chinamax status"},
        },
        monkeypatch,
        capsys,
    )
    assert code == 0
    assert out == ""


def test_silent_on_empty_stdin(monkeypatch, capsys):
    """Empty/unparsable stdin is silent, exit 0."""
    code, out = run_hook({}, monkeypatch, capsys, stdin="")
    assert code == 0
    assert out == ""
