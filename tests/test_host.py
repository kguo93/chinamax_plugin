from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from chinamax import state
from chinamax.host import Host, HostContext, HostResolutionError, resolve_host


def test_host_resolution_precedence_and_fail_closed():
    claude = {
        "CLAUDE_PLUGIN_ROOT": "/claude/plugin",
        "CLAUDE_PLUGIN_DATA": "/claude/data",
    }
    codex = {
        **claude,
        "PLUGIN_ROOT": "/codex/plugin",
        "PLUGIN_DATA": "/codex/data",
    }
    assert resolve_host("claude", codex).host is Host.CLAUDE
    assert resolve_host(None, {"CHINAMAX_HOST": "codex", **claude}).host is Host.CODEX
    assert resolve_host(None, codex).host is Host.CODEX
    assert resolve_host(None, claude).host is Host.CLAUDE
    with pytest.raises(HostResolutionError):
        resolve_host(None, {})
    with pytest.raises(HostResolutionError):
        resolve_host(None, {"CHINAMAX_HOST": "other", **claude})


def test_host_context_never_crosses_data_or_home_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    claude = resolve_host(
        "claude",
        {
            "HOME": str(tmp_path / "home"),
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "claude-data"),
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
        },
    )
    codex = resolve_host(
        "codex",
        {
            "HOME": str(tmp_path / "home"),
            "PLUGIN_DATA": str(tmp_path / "codex-data"),
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
        },
    )
    assert claude.data_root == tmp_path / "claude-data"
    assert claude.state_root == tmp_path / "claude-data" / "state"
    assert claude.keys_path == tmp_path / "home" / ".claude" / "model-keys.env"
    assert codex.data_root == tmp_path / "codex-data"
    assert codex.state_root == tmp_path / "codex-data" / "state"
    assert codex.keys_path == tmp_path / "home" / ".codex" / "model-keys.env"
    assert claude.state_root != codex.state_root


def test_codex_rejects_hostless_records(tmp_path):
    context = HostContext(
        host=Host.CODEX,
        plugin_root=None,
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        keys_path=tmp_path / "keys",
        overlay_path=tmp_path / "overlay",
        interpreter_path=tmp_path / "python-path",
    )
    store = state.JobStore(tmp_path / "store", host_context=context)
    store.ensure()
    record = state.new_record(
        "task-test-abc123",
        prompt="test",
        profile="deepseek",
        write=True,
        workspace_root=tmp_path,
        log_file=store.log_path("task-test-abc123"),
        host=None,
    )
    record.pop("host", None)
    store.record_path(record["id"]).write_text(json.dumps(record), encoding="utf-8")
    assert store.try_read(record["id"]) is None


def test_interpreter_shim_uses_host_specific_data_roots(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "_interpreter.sh"
    base = os.environ.copy()
    for name in ("CHINAMAX_HOST", "CLAUDE_PLUGIN_DATA", "PLUGIN_DATA", "XDG_STATE_HOME"):
        base.pop(name, None)
    base["HOME"] = str(tmp_path / "home")
    base["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "claude-data")
    claude = subprocess.run(
        ["bash", "-c", f"source {script}; chinamax_data_root"],
        env=base,
        capture_output=True,
        text=True,
        check=True,
    )
    codex_env = {**base, "PLUGIN_DATA": str(tmp_path / "codex-data")}
    codex = subprocess.run(
        ["bash", "-c", f"source {script}; chinamax_data_root"],
        env=codex_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert claude.stdout.strip() == str(tmp_path / "claude-data")
    assert codex.stdout.strip() == str(tmp_path / "codex-data")
