"""SessionStart hook: the digest, its bounds, corrupt-state resilience, exports.

Every test invokes the real entrypoint with crafted stdin JSON and reads its
STDOUT — the channel Claude Code injects as session context.
"""

from __future__ import annotations

import io
import json
import os
import subprocess

import pytest

from chinamax import state
from chinamax.hooks import session_start
from conftest import aged, build_record, dead_pid


def run_start(event, monkeypatch, capsys, stdin=None):
    """Run the SessionStart entrypoint with crafted stdin; return (code, stdout)."""
    text = json.dumps(event) if stdin is None else stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = session_start.main()
    return code, capsys.readouterr().out


@pytest.fixture
def workspace_store(tmp_path, keyless_home, monkeypatch):
    """A git workspace whose Job store lives under a temp CLAUDE_PLUGIN_DATA."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    plugin_data = tmp_path / "plugin-data"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    root = state.resolve_workspace_root(workspace)
    store = state.JobStore(state.state_root() / state.workspace_key(root), workspace_root=root)
    return workspace, root, store


def test_digest_running_recent(workspace_store, monkeypatch, capsys):
    """The digest names active + interrupted + the 5 most-recent finished, bounded."""
    workspace, root, store = workspace_store

    running = build_record(store, workspace=root, status=state.STATUS_RUNNING)
    # A dead pid with an aged heartbeat derives `interrupted` read-side (never a
    # stored status the store could produce).
    interrupted = build_record(
        store,
        workspace=root,
        status=state.STATUS_RUNNING,
        pid=dead_pid(),
        updated_at=aged(120),
    )
    completed = build_record(
        store, workspace=root, status=state.STATUS_COMPLETED, completed_at=state.utc_now()
    )
    failed = build_record(
        store, workspace=root, status=state.STATUS_FAILED, completed_at=state.utc_now()
    )
    # 10 more finished, all older, so the finished list caps at 5 within 24 h.
    for index in range(10):
        build_record(
            store,
            workspace=root,
            status=state.STATUS_COMPLETED,
            completed_at=aged(3600 + index * 60),
        )

    code, out = run_start({"cwd": str(workspace), "session_id": "s1"}, monkeypatch, capsys)

    assert code == 0
    # Every active and interrupted Job appears...
    assert running in out
    assert interrupted in out
    # ...the recent completed AND failed do (finished is not narrowed to completed)...
    assert completed in out
    assert failed in out
    # ...the finished list holds at the 5/24h cap, so the rest are summarized...
    assert "(+7 more)" in out
    # ...and the whole digest stays under the ~2 KB cap.
    assert len(out.encode("utf-8")) < 2048


def test_silent_when_empty(workspace_store, monkeypatch, capsys):
    """No Jobs -> no stdout, exit 0."""
    workspace, _root, _store = workspace_store
    code, out = run_start({"cwd": str(workspace)}, monkeypatch, capsys)
    assert code == 0
    assert out == ""


def test_one_bad_record_does_not_hide_others(workspace_store, monkeypatch, capsys):
    """One truncated jobs/<id>.json among healthy records still renders the healthy."""
    workspace, root, store = workspace_store
    healthy = build_record(store, workspace=root, status=state.STATUS_RUNNING)
    # An O_EXCL reservation with no publish yet: an empty (unparsable) record.
    store.reserve_id()

    code, out = run_start({"cwd": str(workspace)}, monkeypatch, capsys)
    assert code == 0
    assert healthy in out


def test_corrupt_index_still_renders(workspace_store, monkeypatch, capsys):
    """A truncated state.json alongside healthy records still renders the digest."""
    workspace, root, store = workspace_store
    healthy = build_record(store, workspace=root, status=state.STATUS_RUNNING)
    # state.json is a derived id cache readers rebuild from the record files;
    # truncating it must not silence the digest (silence here would be a bug).
    store.index_path.write_text("{ this is not json", encoding="utf-8")

    code, out = run_start({"cwd": str(workspace)}, monkeypatch, capsys)
    assert code == 0
    assert healthy in out


def test_env_exports(workspace_store, tmp_path, monkeypatch, capsys):
    """Both exports append to $CLAUDE_ENV_FILE without clobbering; round-trip; and
    neither is written when the variable is unset."""
    workspace, _root, _store = workspace_store
    env_file = tmp_path / "env-file"
    env_file.write_text("# preexisting content\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))

    session = 'he said "hi"\nsecond line'
    run_start({"cwd": str(workspace), "session_id": session}, monkeypatch, capsys)

    written = env_file.read_text(encoding="utf-8")
    assert "# preexisting content" in written  # not clobbered (append)
    assert "export CHINAMAX_SESSION_ID=" in written
    assert "export CLAUDE_PLUGIN_DATA=" in written

    # The quoted export round-trips byte-identically through a real shell.
    round_tripped = subprocess.run(
        ["bash", "-c", f'set -a; source "{env_file}"; printf %s "$CHINAMAX_SESSION_ID"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert round_tripped == session

    # Unset -> nothing appended.
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    before = env_file.read_text(encoding="utf-8")
    run_start({"cwd": str(workspace), "session_id": "s2"}, monkeypatch, capsys)
    assert env_file.read_text(encoding="utf-8") == before


def test_workspace_resolved_from_subdir(workspace_store, tmp_path, monkeypatch, capsys):
    """A stdin cwd in a SUBDIR still finds the repo's Jobs; then CLAUDE_PROJECT_DIR
    and the process cwd resolve in that order when cwd is absent."""
    workspace, root, store = workspace_store
    job = build_record(store, workspace=root, status=state.STATUS_RUNNING)
    subdir = workspace / "pkg" / "inner"
    subdir.mkdir(parents=True)

    # Rung 1: stdin cwd pointing at the subdirectory.
    _code, out = run_start({"cwd": str(subdir)}, monkeypatch, capsys)
    assert job in out

    # Rung 2: no cwd, CLAUDE_PROJECT_DIR names the subdirectory.
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(subdir))
    _code, out = run_start({}, monkeypatch, capsys)
    assert job in out

    # Rung 3: no cwd, no CLAUDE_PROJECT_DIR, the process cwd is the subdirectory.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(subdir)
    _code, out = run_start({}, monkeypatch, capsys)
    assert job in out


def test_orphan_reap_on_start(workspace_store, monkeypatch, capsys):
    """SessionStart reaps a dead-session orphan (interrupted), spares a live session,
    reports (never kills) an ownerless live worker, GCs stale registries, and the
    digest reports the termination while still listing recent terminal Jobs."""
    workspace, root, store = workspace_store
    # Dead session: a dead-pid registry -> its Job is an orphan.
    orphan = build_record(
        store, workspace=root, status=state.STATUS_RUNNING,
        session_id="DEAD", bridge_name="chinamax-glm-orphan",
    )
    state.write_session_registry("DEAD", dead_pid(), None)
    # Live session: registered with this process's pid -> spared.
    live = build_record(
        store, workspace=root, status=state.STATUS_RUNNING, session_id="LIVE",
    )
    state.write_session_registry("LIVE", os.getpid(), state.read_pid_start_time(os.getpid()))
    # No sessionId and no pid -> never gone -> reported, never killed.
    ownerless = build_record(store, workspace=root, status=state.STATUS_RUNNING)
    # A recent terminal Job that must still surface in the digest.
    done = build_record(
        store, workspace=root, status=state.STATUS_COMPLETED, completed_at=state.utc_now(),
    )

    code, out = run_start({"cwd": str(workspace), "session_id": "NEW"}, monkeypatch, capsys)
    assert code == 0

    assert store.read(orphan)["status"] == state.STATUS_INTERRUPTED
    assert store.read(live)["status"] == state.STATUS_RUNNING
    assert store.read(ownerless)["status"] == state.STATUS_RUNNING
    # The dead session's registry is GC'd; the live one and the new one remain.
    assert state.read_session_registry("DEAD") is None
    assert state.read_session_registry("LIVE") is not None
    assert state.read_session_registry("NEW") is not None
    # The digest reports the freshly-interrupted orphan (bridge-first) AND the
    # recent terminal Job (how a clean SessionEnd reap would surface here).
    assert orphan in out
    assert "chinamax-glm-orphan" in out
    assert done in out


def test_supervision_sweep_on_start(workspace_store, monkeypatch, capsys):
    """SessionStart sweeps THIS (live) session's stale-supervised Bridge-owned Job
    dead (interrupted) after the orphan reap, and the digest reports it bridge-first.

    The starting session is registered live first, so the orphan reap spares it;
    the supervision sweep is what reaps the dead Bridge's Job.
    """
    workspace, root, store = workspace_store
    job_id = build_record(
        store, workspace=root, status=state.STATUS_RUNNING,
        session_id="NEW", bridge_name="chinamax-glm-dead",
        supervision_timeout_ms=120_000, supervised_at=aged(300),
    )
    code, out = run_start({"cwd": str(workspace), "session_id": "NEW"}, monkeypatch, capsys)
    assert code == 0
    assert store.read(job_id)["status"] == state.STATUS_INTERRUPTED
    assert job_id in out
    assert "chinamax-glm-dead" in out
