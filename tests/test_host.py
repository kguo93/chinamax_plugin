from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from chinamax import state
import chinamax.host as host_module
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


def test_macos_uses_native_application_support_roots(monkeypatch):
    """macOS fallback roots are Host-isolated and ignore XDG."""
    monkeypatch.setattr(host_module.sys, "platform", "darwin")
    env = {"HOME": "/Users/alice", "XDG_STATE_HOME": "/tmp/xdg"}
    claude = HostContext.from_host(Host.CLAUDE, env)
    codex = HostContext.from_host(Host.CODEX, env)
    assert claude.data_root == Path("/Users/alice/Library/Application Support/chinamax")
    assert codex.data_root == Path(
        "/Users/alice/Library/Application Support/chinamax-codex"
    )
    assert claude.keys_path == Path("/Users/alice/.claude/model-keys.env")
    assert codex.keys_path == Path("/Users/alice/.codex/model-keys.env")


def test_windows_uses_userprofile_and_localappdata(monkeypatch):
    """Windows ignores Git Bash HOME for native roots and uses LOCALAPPDATA."""
    monkeypatch.setattr(host_module.sys, "platform", "win32")
    env = {
        "HOME": "/c/Users/gitbash",
        "USERPROFILE": "/Users/alice",
        "LOCALAPPDATA": "/Users/alice/AppData/Local",
        "XDG_STATE_HOME": "/tmp/xdg",
    }
    claude = HostContext.from_host(Host.CLAUDE, env)
    codex = HostContext.from_host(Host.CODEX, env)
    assert claude.data_root == Path("/Users/alice/AppData/Local/chinamax")
    assert codex.data_root == Path("/Users/alice/AppData/Local/chinamax-codex")
    assert claude.keys_path == Path("/Users/alice/.claude/model-keys.env")
    assert codex.keys_path == Path("/Users/alice/.codex/model-keys.env")


def test_windows_localappdata_falls_back_under_userprofile(monkeypatch):
    monkeypatch.setattr(host_module.sys, "platform", "win32")
    context = HostContext.from_host(
        Host.CLAUDE,
        {"HOME": "/c/Users/gitbash", "USERPROFILE": "/Users/alice"},
    )
    assert context.data_root == Path("/Users/alice/AppData/Local/chinamax")


def test_windows_drive_and_unc_paths_use_native_grammar(monkeypatch):
    """Drive-letter and UNC roots remain absolute under mocked Windows tests."""
    monkeypatch.setattr(host_module.sys, "platform", "win32")
    drive = HostContext.from_host(
        Host.CLAUDE,
        {
            "HOME": "/c/Users/gitbash",
            "USERPROFILE": r"C:\Users\Alice",
            "LOCALAPPDATA": r"C:\Users\Alice\AppData\Local",
        },
    )
    assert str(drive.data_root) == r"C:\Users\Alice\AppData\Local\chinamax"
    assert str(drive.keys_path) == r"C:\Users\Alice\.claude\model-keys.env"

    unc = HostContext.from_host(
        Host.CODEX,
        {
            "HOME": "/c/Users/gitbash",
            "USERPROFILE": r"\\server\share\Users\Alice",
            "LOCALAPPDATA": r"\\server\share\AppData\Local",
        },
    )
    assert str(unc.data_root) == r"\\server\share\AppData\Local\chinamax-codex"

    relative = HostContext.from_host(
        Host.CLAUDE,
        {"USERPROFILE": r"C:\Users\Alice", "LOCALAPPDATA": "relative"},
    )
    assert str(relative.data_root) == r"C:\Users\Alice\AppData\Local\chinamax"


def test_interpreter_shim_windows_native_root_without_cygpath(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "_interpreter.sh"
    env = {
        **os.environ,
        "OS": "Windows_NT",
        "CHINAMAX_HOST": "codex",
        "HOME": "/c/Users/gitbash",
        "USERPROFILE": str(tmp_path / "user"),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }
    result = subprocess.run(
        ["bash", "-c", f"source {script}; chinamax_data_root"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(tmp_path / "local" / "chinamax-codex")


def test_interpreter_shim_macos_routes_to_application_support(tmp_path):
    """On macOS the shim ignores XDG and uses ~/Library/Application Support, matching host.py."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "_interpreter.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uname = bin_dir / "uname"
    fake_uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    fake_uname.chmod(0o700)
    home = tmp_path / "home"
    base_env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(home),
        # XDG is set on purpose and MUST be ignored on macOS (parity with host.py).
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
    }
    # Force the native fallback path: no Windows marker, no Host-provided data root.
    for name in ("OS", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        base_env.pop(name, None)

    def data_root(host_value: str) -> str:
        result = subprocess.run(
            ["bash", "-c", f"source {script}; chinamax_data_root"],
            env={**base_env, "CHINAMAX_HOST": host_value},
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    assert data_root("claude") == str(
        home / "Library" / "Application Support" / "chinamax"
    )
    assert data_root("codex") == str(
        home / "Library" / "Application Support" / "chinamax-codex"
    )
