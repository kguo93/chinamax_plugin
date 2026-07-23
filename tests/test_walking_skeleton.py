"""The walking skeleton: a scripted Job runs tools and terminates on report_result."""

from __future__ import annotations

import pytest

from chinamax.transcript import read_messages
from conftest import (
    BASH_COMMAND,
    BASH_TOOL_USE_ID,
    REPORT_PAYLOAD,
    REPORT_TOOL_USE_ID,
    SYNTHETIC_KEYS,
    bash_then_report_script,
    report_turn,
    text_block,
    tool_use_block,
    turn,
)


def tool_results(messages: list[dict]) -> list[dict]:
    """Return every tool_result block in a message sequence, in order."""
    return [
        block
        for message in messages
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def test_bash_then_report_result(job_env):
    """The loop runs the scripted bash command and stores the payload verbatim."""
    env = job_env(bash_then_report_script())

    assert env.run() == 0

    assert (env.workspace / "out.txt").read_text(encoding="utf-8") == "hello\n"
    assert env.result() == REPORT_PAYLOAD
    assert set(env.result()) == set(REPORT_PAYLOAD)
    assert "exit_code: 0" in tool_results(read_messages(env.transcript_path))[0]["content"]


def test_tool_result_returned_to_provider(job_env):
    """The next request answers the tool_use with its output and exit code."""
    env = job_env(bash_then_report_script())

    assert env.run() == 0

    blocks = tool_results(env.requests[1]["body"]["messages"])
    assert [block["tool_use_id"] for block in blocks] == [BASH_TOOL_USE_ID]
    assert "hello" in blocks[0]["content"]
    assert "exit_code: 0" in blocks[0]["content"]


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens", "stop_sequence"])
def test_tool_less_turn_is_nudged(job_env, stop_reason):
    """A turn with no tool_use is answered with a new user message, whatever stopped it."""
    env = job_env(
        [turn([text_block("Let me think about that.")], stop_reason=stop_reason), report_turn()]
    )

    assert env.run() == 0

    assert env.result() == REPORT_PAYLOAD
    assert len(env.requests) == 2
    messages = env.requests[1]["body"]["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"] != messages[0]["content"]


def test_bearer_auth_and_advertised_tools(job_env, monkeypatch):
    """An ambient ANTHROPIC_API_KEY never reaches the wire, and two tools are advertised."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-must-not-be-used")
    env = job_env(bash_then_report_script())

    assert env.run() == 0

    headers = env.requests[0]["headers"]
    assert headers["authorization"] == f"Bearer {SYNTHETIC_KEYS['DEEPSEEK_API_KEY']}"
    assert "x-api-key" not in headers
    assert [tool["name"] for tool in env.requests[0]["body"]["tools"]] == [
        "bash",
        "report_result",
    ]


def test_report_result_sharing_a_turn(job_env):
    """Siblings of a terminal report_result are executed, answered, and then the run ends."""
    env = job_env(
        [
            turn(
                [
                    tool_use_block(BASH_TOOL_USE_ID, "bash", {"command": BASH_COMMAND}),
                    tool_use_block(REPORT_TOOL_USE_ID, "report_result", REPORT_PAYLOAD),
                ]
            )
        ]
    )

    assert env.run() == 0

    assert (env.workspace / "out.txt").read_text(encoding="utf-8") == "hello\n"
    assert env.result() == REPORT_PAYLOAD
    assert len(env.requests) == 1
    blocks = tool_results(read_messages(env.transcript_path))
    assert [block["tool_use_id"] for block in blocks] == [BASH_TOOL_USE_ID]
