"""The Thread transcript is a replayable, write-ahead record of the conversation."""

from __future__ import annotations

import json

from chinamax.transcript import read_messages
from conftest import BASH_TOOL_USE_ID, bash_then_report_script


def test_transcript_reconstructs_history(job_env):
    """Replaying the JSONL yields the final request's history plus the last turn."""
    env = job_env(bash_then_report_script())

    assert env.run() == 0

    messages = read_messages(env.transcript_path)
    assert messages[:-1] == env.requests[-1]["body"]["messages"]
    assert messages[-1]["role"] == "assistant"
    assert [block["name"] for block in messages[-1]["content"]] == ["report_result"]


def test_outgoing_delta_written_before_call(job_env):
    """The outgoing tool_result is on disk before the request that carries it is sent."""
    env = job_env(bash_then_report_script())

    assert env.run() == 0

    snapshot = env.requests[1]["transcript_snapshot"]
    records = [json.loads(line) for line in snapshot.splitlines() if line.strip()]
    blocks = [
        block
        for record in records
        for block in record["content"]
        if block.get("type") == "tool_result"
    ]
    assert [block["tool_use_id"] for block in blocks] == [BASH_TOOL_USE_ID]
