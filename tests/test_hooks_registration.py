"""hooks.json: no SessionEnd, and the REGISTERED command strings actually run.

`test_registered_commands_run` takes the command strings VERBATIM from
hooks.json, expands ${CLAUDE_PLUGIN_ROOT}, and invokes exactly those against full
crafted event JSON — so the registered path and the shims are what the suite
exercises, not a module the registration never names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from chinamax import state
from conftest import build_record

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))


def _entry(event_name: str) -> dict:
    """Return the single registered hook entry for an event."""
    groups = HOOKS["hooks"][event_name]
    assert len(groups) == 1, groups
    assert len(groups[0]["hooks"]) == 1, groups
    return groups[0]["hooks"][0]


def test_registered_events():
    """hooks.json registers the five session/enforcement events (ADR 0004 reversed).

    Jobs are session-scoped now, so SessionEnd IS registered — the inverse of the
    old no-SessionEnd invariant.
    """
    assert set(HOOKS["hooks"]) == {
        "SessionStart",
        "SessionEnd",
        "Stop",
        "UserPromptSubmit",
        "PreToolUse",
    }

    # The reversal rationale is recorded in hooks.json's own description.
    assert "SessionEnd" in HOOKS["description"]

    # SessionStart's matcher includes both `clear` (re-inject after /clear) and
    # `fork` (a forked session must register its own owner).
    matcher = HOOKS["hooks"]["SessionStart"][0]["matcher"]
    assert "clear" in matcher and "fork" in matcher

    # Timeouts: 10s for everything except SessionEnd (30s, for a multi-Job reap).
    assert _entry("SessionStart")["timeout"] == 10
    assert _entry("Stop")["timeout"] == 10
    assert _entry("UserPromptSubmit")["timeout"] == 10
    assert _entry("PreToolUse")["timeout"] == 10
    assert _entry("SessionEnd")["timeout"] == 30
    assert _entry("SessionStart")["type"] == "command"

    # PreToolUse is scoped to Bash (the Bridge's only tool).
    assert HOOKS["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


def _run_registered(command: str, event: dict, extra_env: dict) -> subprocess.CompletedProcess:
    """Run a registered command string (shell-expanded) against a crafted event."""
    env = os.environ.copy()
    env.update(extra_env)
    return subprocess.run(
        command,
        shell=True,
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_registered_commands_run(tmp_path, keyless_home, monkeypatch):
    """The exact registered command strings run the shims end-to-end."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    plugin_data = tmp_path / "plugin-data"

    extra_env = {
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "CLAUDE_PLUGIN_DATA": str(plugin_data),
        # Resolve the shim's interpreter to this env python (rung 2), so the run
        # does not depend on HOME/miniconda under the temp HOME.
        "CHINAMAX_PYTHON": sys.executable,
    }
    for name in ("XDG_STATE_HOME", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE"):
        extra_env.pop(name, None)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))

    event = {
        "session_id": "reg-smoke",
        "cwd": str(workspace),
        "hook_event_name": "SessionStart",
        "transcript_path": str(workspace / "t.jsonl"),
    }
    start_cmd = _entry("SessionStart")["command"]
    stop_cmd = _entry("Stop")["command"]
    stop_event = {**event, "hook_event_name": "Stop"}

    # Empty state: both hooks exit 0 with no stdout.
    start = _run_registered(start_cmd, event, extra_env)
    assert start.returncode == 0, start.stderr
    assert start.stdout == "", start.stdout
    stop = _run_registered(stop_cmd, stop_event, extra_env)
    assert stop.returncode == 0, stop.stderr
    assert stop.stdout == "", stop.stdout

    # Seed a running Job into the SAME state root the shims read.
    root = state.resolve_workspace_root(workspace)
    store = state.JobStore(state.state_root() / state.workspace_key(root), workspace_root=root)
    running = build_record(store, workspace=root, status=state.STATUS_RUNNING)

    start = _run_registered(start_cmd, event, extra_env)
    assert start.returncode == 0, start.stderr
    assert running in start.stdout

    stop = _run_registered(stop_cmd, stop_event, extra_env)
    assert stop.returncode == 0, stop.stderr
    payload = json.loads(stop.stdout)
    assert set(payload) == {"systemMessage"}
    assert running in payload["systemMessage"]
