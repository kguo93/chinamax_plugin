"""Stop hook: a non-blocking, active-only notice on a single systemMessage key."""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from chinamax import state
from chinamax.hooks import stop
from conftest import aged, build_record, dead_pid


def run_stop(event, monkeypatch, capsys, stdin=None):
    """Run the Stop entrypoint with crafted stdin; return (code, stdout)."""
    text = json.dumps(event) if stdin is None else stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = stop.main()
    return code, capsys.readouterr().out


@pytest.fixture
def make_workspace(tmp_path, keyless_home, monkeypatch):
    """Factory: a fresh git workspace + store under a temp CLAUDE_PLUGIN_DATA."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    counter = {"n": 0}

    def _make():
        counter["n"] += 1
        workspace = tmp_path / f"ws{counter['n']}"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        root = state.resolve_workspace_root(workspace)
        store = state.JobStore(
            state.state_root() / state.workspace_key(root), workspace_root=root
        )
        return workspace, root, store

    return _make


def test_notice_active_only(make_workspace, monkeypatch, capsys):
    """An active Job -> one systemMessage-only JSON object; interrupted/empty -> silent."""
    # An ACTIVE Job: a single JSON object whose ONLY key is systemMessage, naming
    # the id and pointing at /chinamax:status, with NO decision key.
    workspace, root, store = make_workspace()
    running = build_record(store, workspace=root, status=state.STATUS_RUNNING)
    code, out = run_stop({"cwd": str(workspace)}, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out)
    assert set(payload) == {"systemMessage"}
    assert "decision" not in payload
    assert running in payload["systemMessage"]
    assert "/chinamax:status" in payload["systemMessage"]

    # An interrupted-only workspace: interrupted work is not in flight, so Stop
    # stays quiet (SessionStart still surfaces it).
    workspace, root, store = make_workspace()
    build_record(
        store,
        workspace=root,
        status=state.STATUS_RUNNING,
        pid=dead_pid(),
        updated_at=aged(120),
    )
    code, out = run_stop({"cwd": str(workspace)}, monkeypatch, capsys)
    assert code == 0
    assert out == ""

    # Nothing at all: no stdout.
    workspace, root, store = make_workspace()
    code, out = run_stop({"cwd": str(workspace)}, monkeypatch, capsys)
    assert code == 0
    assert out == ""


def test_notice_sweeps_dead_bridge(make_workspace, monkeypatch, capsys):
    """A stale-supervised Job is swept dead before the active filter, so the notice
    no longer lists it (and stays silent when it was the only Job)."""
    workspace, root, store = make_workspace()
    job_id = build_record(
        store, workspace=root, status=state.STATUS_RUNNING,
        session_id="S", bridge_name="chinamax-glm-dead",
        supervision_timeout_ms=120_000, supervised_at=aged(300),
    )
    code, out = run_stop({"cwd": str(workspace), "session_id": "S"}, monkeypatch, capsys)
    assert code == 0
    assert store.read(job_id)["status"] == state.STATUS_INTERRUPTED
    assert out == ""
