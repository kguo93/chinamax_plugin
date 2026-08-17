"""`resume`: a new Job carrying the prior Thread, refused while one is active."""

from __future__ import annotations

import json

from chinamax import state
from chinamax.__main__ import DEFAULT_RESUME_PROMPT, main
from chinamax.transcript import read_messages, write_messages
from conftest import (
    PROFILE,
    REPORT_PAYLOAD,
    REPORT_TOOL_USE_ID,
    aged,
    assert_wire_shape,
    bash_then_report_script,
    build_record,
    report_turn,
    tool_use_block,
    turn,
    wait_for,
    wait_for_status,
    write_overlay,
)
from fake_provider import thinking_block

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


def test_resume_replays_thinking_block_across_boundary(dispatch_env):
    """A source Thread's thinking block replays verbatim in the resumed Job's
    first request, across the Job boundary (thinking is ordinary history)."""
    reasoning = thinking_block("Source reasoning.", "sig-src")
    env = dispatch_env(
        [turn([reasoning, tool_use_block(REPORT_TOOL_USE_ID, "report_result", REPORT_PAYLOAD)])]
    )
    code, source = env.dispatch()
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    finished = wait_for_status(store, source, state.TERMINAL_STATUSES)
    assert finished["status"] == state.STATUS_COMPLETED, finished.get("errorMessage")

    # A fresh provider: the source Job exhausted the first one's script.
    resumed_provider = env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    assert wait_for(lambda: bool(resumed_provider.requests))

    sent = resumed_provider.requests[0]["body"]["messages"]
    replayed = [
        block
        for message in sent
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    assert replayed == [
        {"type": "thinking", "thinking": "Source reasoning.", "signature": "sig-src"}
    ]


def test_resume_replays_pinned_model_over_overlay_edit(dispatch_env, capsys):
    """A pinned model rides every resume verbatim, even when an overlay edit
    changed the Profile's default model between Jobs — only the string is pinned."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    source = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_COMPLETED,
        completed_at=aged(60),
        model="custom-m",
    )
    seed_thread(store, source, "Source thread.")

    # A fresh provider, then an overlay that ALSO edits the Profile's model. The
    # overlay is the endpoint seam too, so the one row carries both fields.
    provider = env.bind([report_turn()])
    write_overlay(
        env.home,
        [{"name": PROFILE, "base_url": provider.base_url, "model": "overlay-model"}],
    )
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    assert record["request"]["model"] == "custom-m"
    assert provider.requests[0]["body"]["model"] == "custom-m"


def test_unpinned_resume_follows_overlay_model_edit(dispatch_env, capsys):
    """The documented re-resolution semantic: an UNpinned Thread's resume picks up
    an overlay's new model — this must not silently change."""
    env = dispatch_env()
    store = env.store
    workspace = str(env.workspace)
    source = build_record(
        store,
        workspace=env.workspace,
        status=state.STATUS_COMPLETED,
        completed_at=aged(60),
    )
    seed_thread(store, source, "Source thread.")

    provider = env.bind([report_turn()])
    write_overlay(
        env.home,
        [{"name": PROFILE, "base_url": provider.base_url, "model": "overlay-model"}],
    )
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    assert "model" not in record["request"]
    assert provider.requests[0]["body"]["model"] == "overlay-model"


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


# ── Worker Host-policy across resume (ADR 0016) ────────────────────────────────

import os as _os

from chinamax.policy import _MEMORY_OPEN
from conftest import (
    memory_block_paths,
    mcp_server_entry,
    mcp_server_script,
    write_mcp_config,
    write_policy_settings,
)


def test_mcp_selection_replayed_on_resume(dispatch_env, capsys):
    """A Thread's pinned Worker-MCP selection is replayed verbatim on resume.

    The pin is the CONCRETE list of discovered server names, resolved once at
    dispatch; the file can flip OFF afterward and the resume still replays the
    same names (resumes never re-read settings).
    """
    env = dispatch_env(bash_then_report_script())
    write_policy_settings(mcp=True)
    (env.workspace / ".claude").mkdir()
    script = mcp_server_script(env.workspace)
    write_mcp_config(
        env.workspace / ".mcp.json",
        {"foo": mcp_server_entry(script), "bar": mcp_server_entry(script)},
    )
    code, source = env.dispatch()
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    finished = wait_for_status(store, source, state.TERMINAL_STATUSES)
    assert finished["status"] == state.STATUS_COMPLETED, finished.get("errorMessage")
    assert finished["request"]["mcp"] == ["foo", "bar"]

    # The file flips OFF, but the pin rides the Thread — resumes never re-read it.
    write_policy_settings(mcp=False)
    env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    # The pin rides the Thread: a resume replays exactly the same names.
    assert record["request"]["mcp"] == ["foo", "bar"]


def test_legacy_record_without_pins_resumes_off(dispatch_env, capsys):
    """A legacy record with NO mcp/boolean pins resumes fully OFF, never all-discovered."""
    env = dispatch_env(bash_then_report_script())
    (env.workspace / ".claude").mkdir()
    script = mcp_server_script(env.workspace)
    write_mcp_config(env.workspace / ".mcp.json", {"echo": mcp_server_entry(script)})
    code, source = env.dispatch()
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    wait_for_status(store, source, state.TERMINAL_STATUSES)
    # Simulate a pre-0.7 record: strip every pin the dispatch wrote (the mcp list
    # AND the memoryEnabled/hooksEnabled booleans).
    record = store.read(source)
    request = dict(record["request"])
    for key in ("mcp", "memoryEnabled", "hooksEnabled"):
        request.pop(key, None)
    assert store.update(source, {"request": request}, expect={record["status"]}) is not None

    resumed_provider = env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    assert wait_for(lambda: bool(resumed_provider.requests))
    resumed = capsys.readouterr().out.strip()
    completed = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert completed["status"] == state.STATUS_COMPLETED, completed.get("errorMessage")
    # The absent mcp pin coerces to [] (OFF): no server is connected on resume,
    # never the None⇒all-discovered arm.
    names = [tool["name"] for tool in resumed_provider.requests[0]["body"]["tools"]]
    assert not any(name.startswith("mcp__") for name in names)
    # The absent booleans default False (None-drop → parse_spec default), and the
    # resumed record re-pins them OFF.
    assert completed["request"]["memoryEnabled"] is False
    assert completed["request"]["hooksEnabled"] is False


def test_policy_booleans_replayed_on_resume(dispatch_env, capsys):
    """The pinned memory/hooks booleans ride the Thread — a later file flip is ignored."""
    env = dispatch_env(bash_then_report_script())
    write_policy_settings(memory=True, hooks=True)
    code, source = env.dispatch()
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    finished = wait_for_status(store, source, state.TERMINAL_STATUSES)
    assert finished["request"]["memoryEnabled"] is True
    assert finished["request"]["hooksEnabled"] is True

    # Flip the file OFF; the resume replays the ON pins (resumes never re-read).
    write_policy_settings(memory=False, hooks=False)
    env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["request"]["memoryEnabled"] is True
    assert record["request"]["hooksEnabled"] is True


def test_policy_booleans_off_stay_off_on_resume(dispatch_env, capsys):
    """The vice-versa: OFF pins stay OFF after the file flips ON."""
    env = dispatch_env(bash_then_report_script())
    code, source = env.dispatch()  # no settings file → all OFF
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    finished = wait_for_status(store, source, state.TERMINAL_STATUSES)
    assert finished["request"]["memoryEnabled"] is False
    assert finished["request"]["hooksEnabled"] is False

    write_policy_settings(memory=True, hooks=True)
    env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    resumed = capsys.readouterr().out.strip()
    record = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert record["request"]["memoryEnabled"] is False
    assert record["request"]["hooksEnabled"] is False


def test_memory_not_reinjected_on_resume(dispatch_env, capsys):
    """A resumed Job never re-injects the Memory chain; the source block stands once."""
    env = dispatch_env(bash_then_report_script())
    write_policy_settings(memory=True)
    (env.workspace / "CLAUDE.md").write_text("Workspace rule.", encoding="utf-8")
    code, source = env.dispatch()
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    wait_for_status(store, source, state.TERMINAL_STATUSES)

    resumed_provider = env.bind([report_turn()])
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    assert wait_for(lambda: bool(resumed_provider.requests))
    resumed_id = capsys.readouterr().out.strip()
    wait_for_status(store, resumed_id, state.TERMINAL_STATUSES)

    # Exactly ONE injection block — the source's — is carried; none is minted fresh.
    sent = json.dumps(resumed_provider.requests[0]["body"]["messages"])
    assert sent.count(_MEMORY_OPEN) == 1


def test_lazy_set_derived_from_replayed_transcript(dispatch_env, capsys):
    """A subdir Memory file injected in the source is not re-injected on resume."""
    from conftest import report_turn as _report_turn
    from conftest import tool_use_block as _tool_use_block
    from conftest import turn as _turn

    env = dispatch_env(bash_then_report_script())
    write_policy_settings(memory=True)
    sub = env.workspace / "sub"
    sub.mkdir()
    (sub / "notes.txt").write_text("data\n", encoding="utf-8")
    (sub / "CLAUDE.md").write_text("Subdir rule.", encoding="utf-8")
    # Rebind the source provider to a script that touches sub first, then reports.
    source_provider = env.bind(
        [_turn([_tool_use_block("toolu_r", "read_file", {"path": "sub/notes.txt"})]), _report_turn()]
    )
    code, source = env.dispatch()
    assert code == 0
    store = env.store
    workspace = str(env.workspace)
    wait_for_status(store, source, state.TERMINAL_STATUSES)
    sub_claude = _os.path.realpath(sub / "CLAUDE.md")
    # The source injected the subdir Memory once, lazily, on the touch.
    source_paths = [
        path
        for message in read_messages(store.transcript_path(source))
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "text"
        for path in memory_block_paths(block["text"])
    ]
    assert source_paths.count(sub_claude) == 1

    # The resumed Job touches sub again but must NOT re-inject (set seeded from
    # the replayed transcript).
    env.bind(
        [_turn([_tool_use_block("toolu_r2", "read_file", {"path": "sub/notes.txt"})]), _report_turn()]
    )
    assert main(["resume", "--workspace", workspace, source, "--", FOLLOW_UP]) == 0
    resumed_id = capsys.readouterr().out.strip()
    wait_for_status(store, resumed_id, state.TERMINAL_STATUSES)
    resumed_paths = [
        path
        for message in read_messages(store.transcript_path(resumed_id))
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "text"
        for path in memory_block_paths(block["text"])
    ]
    # Still exactly one — the block carried from the source, never a fresh one.
    assert resumed_paths.count(sub_claude) == 1
