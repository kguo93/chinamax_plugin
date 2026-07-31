"""UserPromptSubmit hook: the live-Bridge roster for THIS session's bridges.

Every test invokes the real entrypoint with crafted stdin JSON and reads its
STDOUT — the additionalContext envelope Claude Code injects into the main turn.
"""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from chinamax import state
from chinamax.hooks import user_prompt
from conftest import aged, build_record


def run_prompt(event, monkeypatch, capsys, stdin=None):
    """Run the user_prompt entrypoint with crafted stdin; return (code, stdout)."""
    text = json.dumps(event) if stdin is None else stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = user_prompt.main()
    return code, capsys.readouterr().out


@pytest.fixture
def prompt_store(tmp_path, keyless_home, monkeypatch):
    """A git workspace whose Job store lives under a temp CLAUDE_PLUGIN_DATA."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    root = state.resolve_workspace_root(workspace)
    store = state.JobStore(state.state_root() / state.workspace_key(root), workspace_root=root)
    return workspace, root, store


def test_roster_lists_this_sessions_bridges(prompt_store, monkeypatch, capsys):
    """One row per Bridge (newest Job wins, terminal included); other sessions omitted."""
    _workspace, root, store = prompt_store
    # Two Jobs for one Bridge (only the newest counts) and a second Bridge, all
    # owned by session S.
    build_record(
        store, workspace=root, status=state.STATUS_COMPLETED,
        completed_at=state.utc_now(), session_id="S", bridge_name="chinamax-glm-a",
    )
    build_record(
        store, workspace=root, status=state.STATUS_RUNNING,
        session_id="S", bridge_name="chinamax-kimi-b",
    )
    # A different session's Bridge must NOT appear.
    build_record(
        store, workspace=root, status=state.STATUS_RUNNING,
        session_id="OTHER", bridge_name="chinamax-mimo-c",
    )

    code, out = run_prompt({"session_id": "S", "prompt": "hi"}, monkeypatch, capsys)
    assert code == 0
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    ctx = hso["additionalContext"]
    assert "chinamax-glm-a (idle" in ctx      # a terminal Bridge is idle/messageable
    assert "chinamax-kimi-b (running)" in ctx  # an active Bridge shows its status
    assert "chinamax-mimo-c" not in ctx        # a different session's Bridge is omitted
    assert "the bridge/worker" in ctx          # the explicit-addressing routing rule


def test_roster_reaps_dead_bridge(prompt_store, monkeypatch, capsys):
    """A stale-supervised Job is swept dead before the roster renders: the record
    ends `interrupted` and its row uses the dead-Bridge rendering, never the idle
    "message it to follow up" advice."""
    _workspace, root, store = prompt_store
    job_id = build_record(
        store, workspace=root, status=state.STATUS_RUNNING,
        session_id="S", bridge_name="chinamax-glm-dead",
        supervision_timeout_ms=120_000, supervised_at=aged(300),
    )
    code, out = run_prompt({"session_id": "S", "prompt": "hi"}, monkeypatch, capsys)
    assert code == 0
    assert store.read(job_id)["status"] == state.STATUS_INTERRUPTED
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "chinamax-glm-dead (dead — bridge terminated" in ctx
    assert "message it to follow up" not in ctx


def test_silent_without_bridges(prompt_store, monkeypatch, capsys):
    """A session that owns no bridge-named Jobs produces no output at all."""
    _workspace, root, store = prompt_store
    # A Job for THIS session but with NO bridgeName is not a roster entry.
    build_record(store, workspace=root, status=state.STATUS_RUNNING, session_id="S")
    code, out = run_prompt({"session_id": "S", "prompt": "hi"}, monkeypatch, capsys)
    assert code == 0
    assert out == ""
