"""`resume`: a new Job carrying the prior Thread, refused while one is active."""

from __future__ import annotations

import json

from chinamax import state
from chinamax.__main__ import DEFAULT_RESUME_PROMPT, main
from chinamax.transcript import read_messages, write_messages
from conftest import (
    PROFILE,
    REPORT_TOOL_USE_ID,
    aged,
    assert_wire_shape,
    bash_then_report_script,
    build_record,
    report_turn,
    wait_for_status,
)

FOLLOW_UP = "Now summarize what you changed."


def seed_thread(store, job_id: str, text: str) -> None:
    """Give a Job a one-turn Thread, written through the production writer."""
    write_messages(
        state.precreate(store.transcript_path(job_id)),
        [{"role": "user", "content": [{"type": "text", "text": text}]}],
    )


def text_blocks(message: dict) -> list[str]:
    """Return a message's text blocks."""
    return [
        block["text"]
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    ]


def test_resume_continues_thread(dispatch_env, capsys):
    """The resumed Job's first request carries the source's FULL history."""
    env = dispatch_env(bash_then_report_script())
    # --read-only so the inherited posture is observable on the new record.
    code, source = env.dispatch("--read-only")
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    finished = wait_for_status(store, source, state.TERMINAL_STATUSES)
    assert finished["status"] == state.STATUS_COMPLETED, finished.get("errorMessage")
    before = read_messages(store.transcript_path(source))

    # A fresh provider: the source Job exhausted the first one's script.
    provider = env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    assert resumed.startswith("task-") and resumed != source
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    sent = provider.requests[0]["body"]["messages"]
    assert_wire_shape(sent)
    assert sent[: len(before)] == before, "the prior history was not carried whole"
    # The terminal report_result turn is PRESERVED and answered synthetically,
    # not dropped — dropping it would delete the source's own report.
    reported = [
        block
        for message in sent
        for block in message["content"]
        if block.get("type") == "tool_use" and block.get("name") == "report_result"
    ]
    assert [block["id"] for block in reported] == [REPORT_TOOL_USE_ID]
    # The follow-up is the newest user message, merged into the turn carrying
    # that synthetic tool_result rather than appended as a second user turn.
    assert sent[-1]["role"] == "user"
    assert text_blocks(sent[-1]) == [FOLLOW_UP]
    assert REPORT_TOOL_USE_ID in {
        block.get("tool_use_id") for block in sent[-1]["content"]
    }

    # The source's own Thread is untouched on disk.
    assert read_messages(store.transcript_path(source)) == before
    # Profile and write posture are inherited, never silently changed.
    assert record["profile"] == PROFILE
    assert record["write"] is False
    assert record["request"]["write"] is False
    assert record["request"]["workspaceRoot"] == finished["request"]["workspaceRoot"]


def test_resume_refuses_while_active(dispatch_env, capsys):
    """Resume refuses while any Job in the workspace is still active."""
    env = dispatch_env()
    store = env.store
    active = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING)

    assert main(["resume", "--workspace", str(env.workspace), "--", FOLLOW_UP]) == 1

    refusal = capsys.readouterr().err
    assert active in refusal
    assert "still running" in refusal
    assert store.job_ids() == [active], "a refused resume must create no Job"


def test_bare_resume_takes_the_latest_thread(dispatch_env, capsys):
    """With no id the newest resumable Thread wins, and the prompt has a default."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    older = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_COMPLETED,
        completed_at=aged(600),
    )
    newer = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_COMPLETED,
        completed_at=aged(60),
    )
    seed_thread(store, older, "Older thread.")
    seed_thread(store, newer, "Newer thread.")

    # A named-but-unknown target is a resolution error, not a silent default.
    assert main(["resume", "--workspace", workspace, "task-nope-000000", "--", FOLLOW_UP]) == 1
    assert "no Job matching" in capsys.readouterr().err

    provider = env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace]) == 0
    resumed = capsys.readouterr().out.strip()
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    sent = json.dumps(provider.requests[0]["body"]["messages"])
    assert "Newer thread." in sent
    assert "Older thread." not in sent
    assert DEFAULT_RESUME_PROMPT in sent
