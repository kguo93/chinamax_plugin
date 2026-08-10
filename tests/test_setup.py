"""The `/chinamax:setup` doctor, driven hermetically with injected probes (ADR 0011).

The conda/import probes AND the fixers are INJECTED so the doctor never resolves
this machine's real conda env — and never runs a real ``conda create`` — while
HOME / CLAUDE_PLUGIN_DATA / XDG_STATE_HOME are all controlled so the state root
and the scaffolded key template are isolated. Any test whose diagnosed env is
MISSING must inject ``create_env``/``install_deps``: the production fixer would
otherwise run ``conda create`` on the machine running the suite.
"""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from chinamax import doctor, profiles
from conftest import write_keys

SENTINEL_KEY = "sk-do-not-print-this-value"


def _ns(json_flag: bool, workspace) -> argparse.Namespace:
    """Build the parsed ``setup`` arguments."""
    return argparse.Namespace(json=json_flag, workspace=str(workspace))


def _all_present(_python: str) -> dict:
    """A dep checker that would report every dep importable (to prove non-use)."""
    return {name: True for name in doctor.DEPS}


def test_platform_dependency_and_prerequisite_matrix(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    assert "psutil" in doctor.required_deps()
    assert "filelock" not in doctor.required_deps()
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    assert {"psutil", "filelock"}.issubset(doctor.required_deps())
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None if name == "cygpath" else f"/bin/{name}")
    assert doctor.prerequisite_status() == {"bash": True, "git": True, "cygpath": False}


class _Fixers:
    """Recording fixer stubs — every env-missing run_setup call injects these."""

    def __init__(self, create_ok: bool = True):
        self.creates = 0
        self.installs: list[str] = []
        self.create_ok = create_ok

    def create_env(self) -> tuple:
        self.creates += 1
        if self.create_ok:
            return True, ""
        return False, "conda not found — install Miniconda, then re-run /chinamax:setup"

    def install_deps(self, env_python: str) -> tuple:
        self.installs.append(env_python)
        return True, ""


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
        fixers = _Fixers(create_ok=False)
        code = doctor.run_setup(
            _ns(True, workspace),
            find_env_python=lambda: None,
            check_deps=_all_present,
            create_env=fixers.create_env,
            install_deps=fixers.install_deps,
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
            "fixes",
        }
        assert report["ok"] is False and code == 1
        assert report["python"] is None
        assert report["env"] == {"present": False, "path": None}
        # Env absent -> deps reported missing WITHOUT running the (True) checker.
        assert report["deps"] == {name: False for name in doctor.DEPS}
        assert report["state_writable"] is False
        # Both the root AND the per-workspace dir are named.
        assert report["state_root"]
        assert report["workspace_state_dir"].startswith(report["state_root"])

        # Per-Profile key presence, one PRESENT and the rest MISSING, by name.
        by_name = {row["name"]: row for row in report["profiles"]}
        assert by_name["deepseek"]["key"] == "PRESENT"
        assert by_name["deepseek"]["key_env"] == "DEEPSEEK_API_KEY"
        assert any(row["key"] == "MISSING" for row in report["profiles"])

        # The failed create is one recorded fix row; the keys file exists, so no
        # template row, and no python means install is never reached.
        assert [row["action"] for row in report["fixes"]] == ["create-env"]
        assert report["fixes"][0]["ok"] is False
        assert "conda not found" in report["fixes"][0]["detail"]
        assert fixers.creates == 1 and fixers.installs == []

        # No key VALUE on any stream, JSON included.
        assert SENTINEL_KEY not in out.out
        assert SENTINEL_KEY not in out.err

        # The human pass reports the same in one go, with the create advice and
        # the failed fix surfaced.
        doctor.run_setup(
            _ns(False, workspace),
            find_env_python=lambda: None,
            check_deps=_all_present,
            create_env=_Fixers(create_ok=False).create_env,
            install_deps=_Fixers().install_deps,
        )
        human = capsys.readouterr()
        assert "MISSING" in human.out and "conda create -y -n chinamax" in human.out
        assert "pip install -e" in human.out
        assert "FAILED" in human.out and "install Miniconda" in human.out
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
        create_env=_Fixers(create_ok=False).create_env,
        install_deps=_Fixers().install_deps,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert report["env"]["present"] is False
    assert report["deps"]["chinamax"] is False


def test_setup_scaffolds_key_template(tmp_path, keyless_home, isolated, capsys):
    """A missing ~/.claude/model-keys.env is scaffolded as a comments-only
    template — one commented line per shipped Profile plus the overlay
    extension recipe, 0600 — and an existing file is NEVER touched."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_python = _fake_python(tmp_path)
    fixers = _Fixers()
    # keyless_home seeds a synthetic key file; this test needs it ABSENT.
    path = profiles.keys_path()
    path.unlink()

    code = doctor.run_setup(
        _ns(True, workspace),
        find_env_python=lambda: fake_python,
        check_deps=_all_present,
        create_env=fixers.create_env,
        install_deps=fixers.install_deps,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert [row["action"] for row in report["fixes"]] == ["key-template"]
    assert fixers.creates == 0 and fixers.installs == []
    content = path.read_text(encoding="utf-8")
    # Comments only: an untouched template parses to zero keys.
    assert all(line.startswith("#") for line in content.splitlines() if line.strip())
    assert profiles.load_keys() == {}
    # One commented `<api_key_env>=` line per shipped Profile.
    for env_name in (
        "DEEPSEEK_API_KEY",
        "MIMO_API_KEY",
        "GLM_API_KEY",
        "MINIMAX_API_KEY",
        "KIMI_API_KEY",
    ):
        assert f"# {env_name}=" in content
    # The extension recipe: overlay row + key line, for any Anthropic-compatible model.
    assert "chinamax-profiles.json" in content
    assert "Anthropic-compatible" in content
    assert "request_extras" in content
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    # An existing file is never overwritten — and a healthy re-run fixes nothing.
    path.write_text("DEEPSEEK_API_KEY=mine\n", encoding="utf-8")
    doctor.run_setup(
        _ns(False, workspace),
        find_env_python=lambda: fake_python,
        check_deps=_all_present,
        create_env=fixers.create_env,
        install_deps=fixers.install_deps,
    )
    human = capsys.readouterr()
    assert path.read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=mine\n"
    assert "fixing" not in human.out
    assert fixers.creates == 0 and fixers.installs == []


def test_setup_bootstraps_env_and_installs(tmp_path, keyless_home, isolated, capsys):
    """Env missing -> create; deps missing -> install under the freshly resolved
    python; the re-diagnosis reports the fixed state and exit 0."""
    write_keys(keyless_home, {"DEEPSEEK_API_KEY": "x"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_python = _fake_python(tmp_path)
    world = {"env": False, "deps": False}
    installs: list[str] = []

    def find_env_python():
        return fake_python if world["env"] else None

    def check_deps(_python: str) -> dict:
        return {name: world["deps"] for name in doctor.DEPS}

    def create_env() -> tuple:
        world["env"] = True
        return True, ""

    def install_deps(env_python: str) -> tuple:
        installs.append(env_python)
        world["deps"] = True
        return True, ""

    code = doctor.run_setup(
        _ns(True, workspace),
        find_env_python=find_env_python,
        check_deps=check_deps,
        create_env=create_env,
        install_deps=install_deps,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 0 and report["ok"] is True
    assert [row["action"] for row in report["fixes"]] == ["create-env", "install-deps"]
    assert all(row["ok"] for row in report["fixes"])
    # The install ran under the python the create made resolvable.
    assert installs == [fake_python]
    assert report["env"] == {"present": True, "path": fake_python}
    assert report["deps"] == {name: True for name in doctor.DEPS}
    # The re-diagnosis recorded the interpreter where the shims read it first.
    assert doctor.python_path_file().read_text(encoding="utf-8").strip() == fake_python


def test_setup_conda_absent_is_bounded(tmp_path, keyless_home, isolated, capsys):
    """An absent conda is one reported failure with advice — install is never
    reached, nothing retries, and the exit stays 1."""
    write_keys(keyless_home, {"DEEPSEEK_API_KEY": "x"})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fixers = _Fixers(create_ok=False)

    code = doctor.run_setup(
        _ns(False, workspace),
        find_env_python=lambda: None,
        check_deps=_all_present,
        create_env=fixers.create_env,
        install_deps=fixers.install_deps,
    )
    out = capsys.readouterr()

    assert code == 1
    assert fixers.creates == 1 and fixers.installs == []
    assert "FAILED" in out.out
    assert "conda not found" in out.out and "install Miniconda" in out.out


def _fake_python(tmp_path, name: str = "fakepy") -> str:
    """Create an absolute, executable stub interpreter and return its path."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return str(path)
