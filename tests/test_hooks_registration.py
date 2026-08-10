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
CODEX_HOOKS = json.loads(
    (REPO_ROOT / "hooks" / "codex-hooks.json").read_text(encoding="utf-8")
)


def _entry(event_name: str) -> dict:
    """Return the single registered hook entry for an event."""
    groups = HOOKS["hooks"][event_name]
    assert len(groups) == 1, groups
    assert len(groups[0]["hooks"]) == 1, groups
    return groups[0]["hooks"][0]


def test_registered_events():
    """hooks.json registers lifecycle and Host-specific enforcement events.

    Jobs are session-scoped now, so SessionEnd IS registered — the inverse of the
    old no-SessionEnd invariant.
    """
    assert set(HOOKS["hooks"]) == {
        "SessionStart",
        "SessionEnd",
        "Stop",
        "UserPromptSubmit",
        "PreToolUse",
        "SubagentStart",
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
    assert all(
        hook["timeout"] == 10
        for group in HOOKS["hooks"]["PreToolUse"]
        for hook in group["hooks"]
    )
    assert _entry("SessionEnd")["timeout"] == 30
    assert _entry("SessionStart")["type"] == "command"

    # PreToolUse is scoped to Bash (the Bridge's only tool).
    assert HOOKS["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert HOOKS["hooks"]["PreToolUse"][1]["matcher"] == "Agent|Bash|spawn_agent"
    assert HOOKS["hooks"]["SubagentStart"][0]["matcher"] == "chinamax_bridge|chinamax[-_]"


def test_codex_registration_has_cross_platform_command_parity():
    """Codex uses its own registration while preserving Claude event semantics."""
    assert set(CODEX_HOOKS["hooks"]) == set(HOOKS["hooks"])
    for event_name, groups in CODEX_HOOKS["hooks"].items():
        assert len(groups) == len(HOOKS["hooks"][event_name])
        expected_timeouts = {
            hook["timeout"]
            for group in HOOKS["hooks"][event_name]
            for hook in group["hooks"]
        }
        if event_name == "SessionEnd":
            expected_timeouts = {3}
        for group in groups:
            for hook in group["hooks"]:
                assert hook["type"] == "command"
                assert hook["timeout"] in expected_timeouts
                assert hook["commandWindows"].startswith("bash -lc ")
                assert "PLUGIN_ROOT" in hook["commandWindows"]
                assert "cygpath" in hook["commandWindows"]
                assert "exec" in hook["commandWindows"]
                assert "powershell" not in hook["commandWindows"].lower()


def test_codex_windows_commands_convert_drive_root_with_spaces(tmp_path):
    """Every Windows registration reaches its matching shim after cygpath."""
    plugin_root = tmp_path / "plugin root"
    scripts = plugin_root / "scripts"
    scripts.mkdir(parents=True)
    shim_names = {
        "session_start_hook",
        "session_end_hook",
        "stop_hook",
        "user_prompt_hook",
        "bridge_contract_hook",
        "codex_pretool_hook",
    }
    for name in shim_names:
        shim = scripts / name
        shim.write_text("#!/bin/sh\nprintf hook-ok\ncat >/dev/null\n", encoding="utf-8")
        shim.chmod(0o700)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cygpath = bin_dir / "cygpath"
    cygpath.write_text("#!/bin/sh\nprintf '%s\\n' \"$CHINAMAX_CYGPATH_RESULT\"\n", encoding="utf-8")
    cygpath.chmod(0o700)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PLUGIN_ROOT": r"C:\Users\Alice\ChinamaX Plugin",
        "CLAUDE_PLUGIN_ROOT": "ignored-fallback",
        "CHINAMAX_CYGPATH_RESULT": str(plugin_root),
    }
    event = {"hook_event_name": "SessionStart", "session_id": "windows-smoke"}
    commands = {
        hook["commandWindows"]
        for groups in CODEX_HOOKS["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    }
    assert len(commands) == len(shim_names)
    for command in commands:
        result = subprocess.run(
            command,
            shell=True,
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "hook-ok"


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
