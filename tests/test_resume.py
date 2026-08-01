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
    wait_for,
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


def test_resume_request_shares_cache_prefix(dispatch_env):
    """The provider's cache prefix survives the resume boundary intact.

    A resume mints a new Job id for bookkeeping, but no Job id exists in any
    request byte — the Thread's prefix is what the provider caches on. The
    resumed Job's first request must replay the source's last request as its
    prefix, under the same system and tools.
    """
    env = dispatch_env(bash_then_report_script())
    source_provider = env.providers[PROFILE]
    code, source = env.dispatch()
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    finished = wait_for_status(store, source, state.TERMINAL_STATUSES)
    assert finished["status"] == state.STATUS_COMPLETED, finished.get("errorMessage")
    last = source_provider.requests[-1]["body"]

    # A fresh provider: the source Job exhausted the first one's script.
    resumed_provider = env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    assert wait_for(lambda: bool(resumed_provider.requests))
    first = resumed_provider.requests[0]["body"]

    assert first["system"] == last["system"]
    assert first["system"]
    assert first["tools"] == last["tools"]
    assert first["model"] == last["model"]
    assert first["max_tokens"] == last["max_tokens"]
    assert first["messages"][: len(last["messages"])] == last["messages"]


def test_bare_resume_refuses_while_active(dispatch_env, capsys):
    """A BARE resume keeps the workspace-wide refusal (it must guess a target)."""
    env = dispatch_env()
    store = env.store
    active = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING)

    assert main(["resume", "--workspace", str(env.workspace), "--", FOLLOW_UP]) == 1

    refusal = capsys.readouterr().err
    assert active in refusal
    assert "still running" in refusal
    assert store.job_ids() == [active], "a refused resume must create no Job"


def test_explicit_resume_succeeds_while_unrelated_active(dispatch_env, capsys):
    """An explicit-id resume succeeds while an UNRELATED Job is active, records
    `resumedFrom`/`lineageRoot`, and inherits the source's `bridgeName`."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    # An unrelated active Job in the same workspace (its own lineage).
    unrelated = build_record(store, workspace=env.workspace, status=state.STATUS_RUNNING)
    # A finished source with a Thread and a Bridge.
    source = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_COMPLETED,
        completed_at=aged(60),
        bridge_name="chinamax-kimi-fix-auth",
    )
    seed_thread(store, source, "Source thread.")

    env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    assert resumed != unrelated and resumed != source
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    assert record["resumedFrom"] == source
    assert record["lineageRoot"] == source
    assert record["bridgeName"] == "chinamax-kimi-fix-auth"


def test_explicit_resume_refuses_own_lineage_active(dispatch_env, capsys):
    """An explicit-id resume refuses while its OWN lineage still has an active Job."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    source = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_COMPLETED,
        completed_at=aged(120),
    )
    seed_thread(store, source, "Source thread.")
    # An active Job sharing the source's lineage root.
    active = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_RUNNING,
        lineage_root=source,
    )

    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 1
    assert "lineage still running" in capsys.readouterr().err
    assert sorted(store.job_ids()) == sorted([source, active]), "no new Job on a refusal"


def test_resume_refuses_reaped_source(dispatch_env, capsys):
    """A reaped (STORED-interrupted) source is refused by both resume forms."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    reaped = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_INTERRUPTED,
        completed_at=aged(60),
    )
    seed_thread(store, reaped, "Reaped thread.")

    # Explicit form: refused, naming the dead session.
    assert main(["resume", "--workspace", workspace, reaped, "--", FOLLOW_UP]) == 1
    assert "session" in capsys.readouterr().err.lower()

    # Bare form: a reaped Thread is not a resumable candidate either.
    assert main(["resume", "--workspace", workspace, "--", FOLLOW_UP]) == 1
    assert "no finished Job with a Thread" in capsys.readouterr().err
    assert store.job_ids() == [reaped], "a refused resume must create no Job"


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
