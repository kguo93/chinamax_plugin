"""SessionEnd hook: kill the ending session's active Jobs, drop its registry.

Jobs are session-scoped (ADR 0004 reversed). Every test invokes the real
entrypoint with crafted stdin JSON. A SessionEnd hook must never block, so it
always exits 0, and duplicate / back-to-back delivery must be idempotent.
"""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from chinamax import state
from chinamax.hooks import session_end, session_start
from conftest import build_record


def run_end(event, monkeypatch, capsys, stdin=None):
    """Run the SessionEnd entrypoint with crafted stdin; return (code, stdout)."""
    text = json.dumps(event) if stdin is None else stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = session_end.main()
    return code, capsys.readouterr().out


@pytest.fixture
def workspace_store(tmp_path, keyless_home, monkeypatch):
    """A git workspace whose Job store lives under a temp CLAUDE_PLUGIN_DATA."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    root = state.resolve_workspace_root(workspace)
    store = state.JobStore(state.state_root() / state.workspace_key(root), workspace_root=root)
    return workspace, root, store


def test_session_end_reaps_owner_only(workspace_store, monkeypatch, capsys):
    """Session A's active Jobs are cancelled + registry removed; session B untouched."""
    _workspace, root, store = workspace_store
    a = build_record(
        store, workspace=root, status=state.STATUS_RUNNING,
        session_id="A", bridge_name="chinamax-glm-a",
    )
    b = build_record(store, workspace=root, status=state.STATUS_RUNNING, session_id="B")
    state.write_session_registry("A", 424242, None)

    code, _out = run_end({"session_id": "A", "reason": "clear"}, monkeypatch, capsys)
    assert code == 0
    assert store.read(a)["status"] == state.STATUS_CANCELLED
    assert store.read(b)["status"] == state.STATUS_RUNNING
    assert state.read_session_registry("A") is None


def test_session_end_always_exits_zero_and_is_idempotent(workspace_store, monkeypatch, capsys):
    """Always exit 0; duplicate delivery and a SessionStart re-reap change nothing."""
    workspace, root, store = workspace_store
    a = build_record(store, workspace=root, status=state.STATUS_RUNNING, session_id="A")

    # First SessionEnd reaps it.
    assert run_end({"session_id": "A"}, monkeypatch, capsys)[0] == 0
    first = store.read(a)
    assert first["status"] == state.STATUS_CANCELLED

    # A duplicate SessionEnd is a no-op (already terminal), still exit 0.
    assert run_end({"session_id": "A"}, monkeypatch, capsys)[0] == 0
    assert store.read(a) == first

    # A back-to-back SessionStart re-reap is idempotent too (already terminal).
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "NEW", "cwd": str(workspace)})),
    )
    assert session_start.main() == 0
    assert store.read(a)["status"] == state.STATUS_CANCELLED

    # Empty/absent stdin still exits 0, touching nothing.
    assert run_end({}, monkeypatch, capsys, stdin="")[0] == 0
