"""The `/chinamax:setup` doctor, driven hermetically with injected probes (ADR 0011).

The conda/import probes are INJECTED so the doctor never resolves this machine's
real conda or chinamax env, and HOME / CLAUDE_PLUGIN_DATA / XDG_STATE_HOME are all
controlled so the state root is isolated.
"""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from chinamax import doctor
from conftest import write_keys

SENTINEL_KEY = "sk-do-not-print-this-value"


def _ns(json_flag: bool, workspace) -> argparse.Namespace:
    """Build the parsed ``setup`` arguments."""
    return argparse.Namespace(json=json_flag, workspace=str(workspace))


def _all_present(_python: str) -> dict:
    """A dep checker that would report every dep importable (to prove non-use)."""
    return {name: True for name in doctor.DEPS}


@pytest.fixture
def isolated(tmp_path, keyless_home, monkeypatch):
    """Isolate HOME and BOTH state-root env vars; return the plugin-data root."""
    plugin_data = tmp_path / "plugin-data"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plugin_data))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return plugin_data


def test_doctor_reports(tmp_path, keyless_home, isolated, monkeypatch, capsys):
    """One pass reports absent env, missing deps, per-Profile keys, and an
    unwritable state root — with the schema pinned and no key value leaked."""
    # One key present (by NAME only), the rest missing per Profile.
    write_keys(keyless_home, {"DEEPSEEK_API_KEY": SENTINEL_KEY})

    # An unwritable state root: point CLAUDE_PLUGIN_DATA under a 0500 parent.
    readonly = tmp_path / "ro"
    readonly.mkdir()
    os.chmod(readonly, stat.S_IREAD | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(readonly / "blocked"))
    workspace = tmp_path / "ws"
    workspace.mkdir()

    try:
        code = doctor.run_setup(
            _ns(True, workspace),
            find_env_python=lambda: None,
            check_deps=_all_present,
        )
        out = capsys.readouterr()
        report = json.loads(out.out)

        # Schema, exactly.
        assert set(report) == {
            "ok",
            "python",
            "state_root",
            "workspace_state_dir",
            "state_writable",
            "env",
            "deps",
            "profiles",
        }
        assert report["ok"] is False and code == 1
        assert report["python"] is None
        assert report["env"] == {"present": False, "path": None}
        # Env absent -> deps reported missing WITHOUT running the (True) checker.
        assert report["deps"] == {"chinamax": False, "anthropic": False, "pytest": False}
        assert report["state_writable"] is False
        # Both the root AND the per-workspace dir are named.
        assert report["state_root"]
        assert report["workspace_state_dir"].startswith(report["state_root"])

        # Per-Profile key presence, one PRESENT and the rest MISSING, by name.
        by_name = {row["name"]: row for row in report["profiles"]}
        assert by_name["deepseek"]["key"] == "PRESENT"
        assert by_name["deepseek"]["key_env"] == "DEEPSEEK_API_KEY"
        assert any(row["key"] == "MISSING" for row in report["profiles"])

        # No key VALUE on any stream, JSON included.
        assert SENTINEL_KEY not in out.out
        assert SENTINEL_KEY not in out.err

        # The human pass reports the same in one go, with the create advice.
        doctor.run_setup(
            _ns(False, workspace),
            find_env_python=lambda: None,
            check_deps=_all_present,
        )
        human = capsys.readouterr()
        assert "MISSING" in human.out and "conda create -y -n chinamax" in human.out
        assert "pip install -e" in human.out
        assert SENTINEL_KEY not in human.out and SENTINEL_KEY not in human.err
    finally:
        os.chmod(readonly, stat.S_IRWXU)


def test_doctor_ok_path(tmp_path, isolated, monkeypatch, capsys):
    """A fully healthy env reports ok:true, exit 0, and records the interpreter."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_python = _fake_python(tmp_path)

    code = doctor.run_setup(
        _ns(True, workspace),
        find_env_python=lambda: fake_python,
        check_deps=_all_present,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 0 and report["ok"] is True
    assert report["env"] == {"present": True, "path": fake_python}
    assert report["deps"] == {name: True for name in doctor.DEPS}
    assert report["state_writable"] is True
    # The resolved interpreter is recorded where the shims read it first.
    assert doctor.python_path_file().read_text(encoding="utf-8").strip() == fake_python


def test_doctor_rerecords_stale_python(tmp_path, isolated, capsys):
    """A recorded python that no longer resolves is re-recorded; a resolving one
    is trusted."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_python = _fake_python(tmp_path)
    record = doctor.python_path_file()
    record.parent.mkdir(parents=True, exist_ok=True)

    # Stale record (path does not resolve) -> re-recorded to the fresh resolution.
    record.write_text("/nonexistent/bin/python\n", encoding="utf-8")
    doctor.run_setup(
        _ns(True, workspace), find_env_python=lambda: fake_python, check_deps=_all_present
    )
    capsys.readouterr()
    assert record.read_text(encoding="utf-8").strip() == fake_python

    # A resolving record is trusted, not overwritten by a different resolution.
    other = _fake_python(tmp_path, name="fakepy2")
    doctor.run_setup(
        _ns(True, workspace), find_env_python=lambda: other, check_deps=_all_present
    )
    capsys.readouterr()
    assert record.read_text(encoding="utf-8").strip() == fake_python


def test_doctor_bootstrap_without_env(tmp_path, isolated, capsys):
    """Under the bootstrap rung with no env, the env reads ABSENT with exit 1 —
    the doctor never grades its own interpreter's imports."""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    code = doctor.run_setup(
        _ns(True, workspace),
        find_env_python=lambda: None,
        # Would report chinamax importable (it IS, in-process) — must be ignored.
        check_deps=_all_present,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert report["env"]["present"] is False
    assert report["deps"]["chinamax"] is False


def _fake_python(tmp_path, name: str = "fakepy") -> str:
    """Create an absolute, executable stub interpreter and return its path."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return str(path)
