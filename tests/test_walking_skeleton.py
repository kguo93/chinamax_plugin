"""The walking skeleton: a scripted Job runs tools and terminates on report_result."""

from __future__ import annotations

import pytest

from chinamax.loop import (
    READ_ONLY_POSTURE,
    SYSTEM_TEMPLATE,
    WRITE_POSTURE,
    _system_prompt,
)
from chinamax.spec import parse_spec
from chinamax.transcript import read_messages
from conftest import (
    BASH_COMMAND,
    BASH_TOOL_USE_ID,
    PROFILE,
    REPORT_PAYLOAD,
    REPORT_TOOL_USE_ID,
    SYNTHETIC_KEYS,
    Sleeper,
    bash_script,
    bash_then_report_script,
    events_named,
    loop_config,
    report_turn,
    text_block,
    tool_results,
    tool_use_block,
    turn,
    write_overlay,
)
from fake_provider import eof_fault, thinking_block

#: A cache-hit usage reading, mirroring a DeepSeek second request: the cached
#: prefix (768 = 12x64) dominates the small uncached delta.
CACHED_USAGE = {
    "input_tokens": 80,
    "cache_read_input_tokens": 768,
    "cache_creation_input_tokens": 0,
}


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
    """An ambient ANTHROPIC_API_KEY never reaches the wire, and the rich set is advertised."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient-must-not-be-used")
    env = job_env(bash_then_report_script())

    assert env.run() == 0

    headers = env.requests[0]["headers"]
    assert headers["authorization"] == f"Bearer {SYNTHETIC_KEYS['DEEPSEEK_API_KEY']}"
    assert "x-api-key" not in headers
    assert [tool["name"] for tool in env.requests[0]["body"]["tools"]] == [
        "bash",
        "read_file",
        "write_file",
        "str_replace_edit",
        "list_dir",
        "grep",
        "glob",
        "apply_patch",
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


def test_usage_event_per_completed_turn(job_env, capsys):
    """Every completed turn emits one usage event carrying the provider counters."""
    env = job_env(
        [
            turn(
                [tool_use_block(BASH_TOOL_USE_ID, "bash", {"command": BASH_COMMAND})],
                usage={"input_tokens": 848},
            ),
            turn(
                [tool_use_block(REPORT_TOOL_USE_ID, "report_result", REPORT_PAYLOAD)],
                usage=CACHED_USAGE,
            ),
        ]
    )

    assert env.run() == 0

    events = events_named(capsys.readouterr().err, "usage")
    assert len(events) == 2
    assert [event["turn"] for event in events] == [1, 2]
    # The uncached first turn: its cache counter is None and so dropped, not zero.
    assert events[0]["input_tokens"] == 848
    assert "cache_read_input_tokens" not in events[0]
    # The cache-hit second turn: the counters survive the message_delta path,
    # and output_tokens comes from that delta (proving the accumulation).
    assert events[1]["input_tokens"] == 80
    assert events[1]["cache_read_input_tokens"] == 768
    assert events[1]["output_tokens"] == 1


def test_aborted_attempt_emits_usage_once(job_env, capsys):
    """A retried attempt contributes no usage: only the completed turn is counted."""
    env = job_env(
        [
            eof_fault(),
            turn(
                [tool_use_block(BASH_TOOL_USE_ID, "bash", {"command": BASH_COMMAND})],
                usage={"input_tokens": 848},
            ),
            turn(
                [tool_use_block(REPORT_TOOL_USE_ID, "report_result", REPORT_PAYLOAD)],
                usage=CACHED_USAGE,
            ),
        ]
    )

    assert env.run(config=loop_config(Sleeper())) == 0

    err = capsys.readouterr().err
    assert len(env.requests) == 3
    assert len(events_named(err, "retry")) == 1
    events = events_named(err, "usage")
    assert len(events) == 2
    # The completed attempt's usage, never the aborted attempt's default of 1.
    assert events[0]["input_tokens"] == 848


def test_request_prefix_stable_across_turns(job_env):
    """The cache-enabling property: the request prefix only ever grows.

    The one sanctioned tail mutation is a drained steer riding the not-yet-sent
    user turn (ADR 0008); it never edits sent history, so the prefix holds.
    """
    env = job_env(bash_script("echo one > a.txt", "echo two > b.txt"))

    assert env.run() == 0

    bodies = [request["body"] for request in env.requests]
    assert len(bodies) == 3
    for first, second in zip(bodies, bodies[1:]):
        assert first["system"] == second["system"]
        assert first["system"]
        assert first["tools"] == second["tools"]
        assert first["model"] == second["model"]
        assert first["max_tokens"] == second["max_tokens"]
        assert second["messages"][: len(first["messages"])] == first["messages"]
        assert len(second["messages"]) > len(first["messages"])


@pytest.mark.parametrize(
    "extras, present, absent",
    [
        # A `thinking` extra rides through verbatim as a body key.
        (
            {"thinking": {"type": "enabled"}},
            {"thinking": {"type": "enabled"}},
            ["extra_body", "reasoning_effort", "reasoning"],
        ),
        # An `extra_body` extra merges into the body ROOT — the bare key, never
        # an `extra_body` wrapper.
        (
            {"extra_body": {"reasoning_effort": "high"}},
            {"reasoning_effort": "high"},
            ["extra_body", "thinking"],
        ),
        # An explicit empty extras: the no-reasoning request path.
        ({}, {}, ["thinking", "extra_body", "reasoning_effort", "reasoning"]),
    ],
)
def test_request_extras_merged_into_wire_body(job_env, keyless_home, extras, present, absent):
    """A Profile's request_extras reach the wire body: `thinking` verbatim,
    `extra_body` keys merged into the root, and `{}` adds nothing."""
    env = job_env([report_turn()])
    # One overlay row carrying the fake base_url AND the extras: write_overlay
    # replaces the whole file, so the base_url must ride along or the Job would
    # dispatch at the real endpoint.
    write_overlay(
        keyless_home,
        [{"name": PROFILE, "base_url": env.fake.base_url, "request_extras": extras}],
    )

    assert env.run() == 0

    body = env.requests[0]["body"]
    for key, value in present.items():
        assert body[key] == value
    for key in absent:
        assert key not in body


def test_thinking_block_replayed_verbatim_next_turn(job_env, keyless_home):
    """A returned thinking block persists in the Thread and replays verbatim,
    and the request prefix stays stable with extras present."""
    reasoning = thinking_block("Let me reason about this step.", "sig-xyz")
    env = job_env(
        [
            turn([reasoning, tool_use_block(BASH_TOOL_USE_ID, "bash", {"command": BASH_COMMAND})]),
            report_turn(),
        ]
    )
    write_overlay(
        keyless_home,
        [{"name": PROFILE, "base_url": env.fake.base_url,
          "request_extras": {"thinking": {"type": "enabled"}}}],
    )

    assert env.run() == 0

    # Turn 2's request replays the assistant turn's thinking block byte-for-byte,
    # signature included — thinking blocks are ordinary Thread history.
    assistant = env.requests[1]["body"]["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "Let me reason about this step.",
        "signature": "sig-xyz",
    }
    # The growing-prefix guarantee holds with extras present: the stable head and
    # the constant `thinking` extra are identical across turns.
    bodies = [request["body"] for request in env.requests]
    assert len(bodies) == 2
    for first, second in zip(bodies, bodies[1:]):
        assert first["system"] == second["system"]
        assert first["tools"] == second["tools"]
        assert first["thinking"] == second["thinking"] == {"type": "enabled"}
        assert second["messages"][: len(first["messages"])] == first["messages"]
        assert len(second["messages"]) > len(first["messages"])


def test_system_prompt_instructional_body_precedes_variable_slots():
    """The whole instructional body sits before the first per-Job format slot."""
    stable = SYSTEM_TEMPLATE.split("{", 1)[0]
    assert "report_result" in stable
    assert "confined" in stable
    assert "relayed back verbatim" in stable


def test_system_prompt_shared_across_workspaces(tmp_path):
    """Workspace and posture affect only the tail; the instructional head is shared."""

    def render(workspace, write):
        spec = parse_spec(
            {
                "workspace": str(workspace),
                "profile": PROFILE,
                "prompt": "Do the task.",
                "transcript_path": str(workspace / "job.thread.jsonl"),
                "result_path": str(workspace / "job.result.json"),
                "write": write,
            }
        )
        return _system_prompt(spec)

    workspace_a = tmp_path / "ws_a"
    workspace_b = tmp_path / "ws_b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    a = render(workspace_a, True)
    b = render(workspace_b, True)
    # The read-only spec shares workspace_a, so only its posture differs from a.
    c = render(workspace_a, False)

    marker = "Your workspace is"
    head_a, head_b = a.split(marker)[0], b.split(marker)[0]
    assert head_a == head_b, "the workspace path must affect only the tail"
    assert "report_result" in head_a
    # The posture flip touches only the final paragraph: a and c share the same
    # workspace, so everything up through the "Your workspace is ..." line is
    # identical and only the trailing posture differs.
    assert a.endswith(WRITE_POSTURE)
    assert c.endswith(READ_ONLY_POSTURE)
    assert a[: -len(WRITE_POSTURE)] == c[: -len(READ_ONLY_POSTURE)]
