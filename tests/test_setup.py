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
import types

import pytest

from chinamax import doctor, profiles
from conftest import write_keys

SENTINEL_KEY = "sk-do-not-print-this-value"

#: The real conda resolver, captured before the autouse stub replaces it; the
#: precedence / phase-A / codex tests restore or override it explicitly.
_REAL_FIND_CONDA = doctor._find_conda


@pytest.fixture(autouse=True)
def _stub_find_conda(monkeypatch):
    """Report Miniconda PRESENT by default.

    Keeps the module-wide ``run_setup`` tests host-independent so they never stall
    at Phase A on a runner without resolvable conda (ADR 0011). Stubs ``_find_conda``
    (NOT ``prerequisite_status``) so the matrix test's real ``prerequisite_status``
    keeps running with the runner's real ``bash``.
    """
    monkeypatch.setattr(doctor, "_find_conda", lambda: "/stub/miniconda3/bin/conda")


def _ns(json_flag: bool, workspace) -> argparse.Namespace:
    """Build the parsed ``setup`` arguments."""
    return argparse.Namespace(json=json_flag, workspace=str(workspace))


def _all_present(_python: str) -> dict:
    """A dep checker that would report every dep importable (to prove non-use)."""
    return {name: True for name in doctor.DEPS}


def test_platform_dependency_and_prerequisite_matrix(monkeypatch, tmp_path):
    # Linux: bash + miniconda (miniconda = conda-resolvable; stubbed present).
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert doctor.prerequisite_status() == {"bash": True, "miniconda": True}

    # macOS: bash + miniconda (git DROPPED from darwin); psutil dep, not filelock.
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    assert "psutil" in doctor.required_deps()
    assert "filelock" not in doctor.required_deps()
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert doctor.prerequisite_status() == {"bash": True, "miniconda": True}
    # macOS miss: bash absent on PATH -> reported missing (detection, not advice).
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.prerequisite_status() == {"bash": False, "miniconda": True}

    # Windows: probe the Git for Windows install tree on disk, then miniconda.
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    assert {"psutil", "filelock"}.issubset(doctor.required_deps())
    git_root = tmp_path / "Program Files" / "Git"
    for rel in ("cmd/git.exe", "bin/bash.exe", "usr/bin/cygpath.exe"):
        p = git_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    for var in ("ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # no PATH fallback
    status = doctor.prerequisite_status()
    assert status == {"git": True, "bash": True, "cygpath": True, "miniconda": True}
    # Emission order: the Git trio precedes miniconda (its conda init needs bash).
    assert list(status) == ["git", "bash", "cygpath", "miniconda"]

    # cygpath absent on disk but present on PATH -> union still True.
    (git_root / "usr/bin/cygpath.exe").unlink()
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: r"C:\path\cygpath.exe" if name == "cygpath" else None,
    )
    assert doctor.prerequisite_status()["cygpath"] is True

    # cygpath absent on disk AND off PATH -> False (kick-back case).
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.prerequisite_status()["cygpath"] is False

    # Per-user root (%LOCALAPPDATA%\Programs\Git) resolves on its own.
    user_root = tmp_path / "AppData" / "Local" / "Programs" / "Git"
    (user_root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
    (user_root / "usr" / "bin" / "cygpath.exe").write_text("")
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    assert doctor.prerequisite_status()["cygpath"] is True

    # Off-matrix Platform: no prerequisite checks (ADR 0015 target matrix only).
    monkeypatch.setattr(doctor.sys, "platform", "sunos5")
    assert doctor.prerequisite_status() == {}


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

        # Schema, exactly. `prerequisites` is now present UNCONDITIONALLY on Linux
        # ({bash, miniconda}); `prerequisite_fixes` is absent when nothing is missing.
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
            "prerequisites",
        }
        assert report["prerequisites"] == {"bash": True, "miniconda": True}
        assert "prerequisite_fixes" not in report
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
    # Every Prerequisite present -> no rectification section, no Phase-A pause.
    assert report["prerequisites"] == {"bash": True, "miniconda": True}
    assert "prerequisite_fixes" not in report
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
        "QWEN_API_KEY",
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


def test_prerequisite_advice_by_platform(monkeypatch, keyless_home, isolated):
    # Advice now derives from the pre-computed rows, which probe the filesystem —
    # stub shutil.which explicitly and force the winget-ABSENT branch for the URL.
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # winget/pm/brew absent
    rows = doctor.prerequisite_fixes(
        {"git": False, "bash": True, "cygpath": False, "miniconda": True}
    )
    msg = doctor.missing_prerequisite_advice(rows)
    assert doctor._GIT_FOR_WINDOWS_URL in msg
    # Not a bare `"git" in msg` — the URL itself contains "git". The one deduped Git
    # row expands its missing_tools back into the tool names.
    assert "missing prerequisites (git, cygpath)" in msg
    assert msg.count(doctor._GIT_FOR_WINDOWS_URL) == 1  # deduped to one instruction
    assert doctor.missing_prerequisite_advice([]) == ""

    # Claude-host wiring: render_report surfaces the same rectification rows once.
    report = {
        "ok": False,
        "env": {"present": True, "path": "/env/bin/python"},
        "deps": {},
        "profiles": [],
        "state_root": "/tmp/state",
        "workspace_state_dir": "/tmp/state/ws",
        "state_writable": True,
        "prerequisites": {"git": False, "bash": True, "cygpath": False, "miniconda": True},
        "prerequisite_fixes": rows,
    }
    rendered = doctor.render_report(report)
    assert "prerequisite git: MISSING" in rendered
    assert "rectification commands" in rendered
    assert rendered.count(doctor._GIT_FOR_WINDOWS_URL) == 1

    # macOS: bash + miniconda advice; git no longer appears (dropped from darwin).
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)  # brew absent
    mac = doctor.missing_prerequisite_advice(
        doctor.prerequisite_fixes({"bash": False, "miniconda": False})
    )
    assert "brew install bash" in mac and "Xcode" in mac
    assert "Miniconda3-latest-MacOSX-arm64.sh" in mac
    assert "missing prerequisites (bash, miniconda)" in mac


def test_run_codex_setup_reports_missing_prerequisites(monkeypatch, capsys, keyless_home, isolated):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    # The stub plan omits prerequisite_fixes, so run_codex_setup recomputes them;
    # winget absent -> the Git fallback row naming the Git-for-Windows URL.
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    monkeypatch.setattr(
        doctor,
        "codex_setup_plan",
        lambda *a, **k: {"prerequisites": {"bash": True, "git": False, "cygpath": False}},
    )
    # The prerequisite gate lives inside run_codex_setup's apply branch, which
    # first requires yolo (bypassPermissions); the injected fixers/probes are
    # required keyword-only args even though the monkeypatched plan ignores them.
    args = argparse.Namespace(workspace=None, json=False, apply=True)
    code = doctor.run_codex_setup(
        args,
        find_env_python=lambda: None,
        check_deps=_all_present,
        create_env=lambda: (True, ""),
        install_deps=lambda _python: (True, ""),
        permission_mode="bypassPermissions",
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "chinamax:" in err and doctor._GIT_FOR_WINDOWS_URL in err


def test_apply_codex_setup_raises_missing_prerequisites(monkeypatch, keyless_home, isolated):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    # The stub plan omits prerequisite_fixes, so apply_codex_setup recomputes them;
    # winget absent -> the Git fallback row naming the Git-for-Windows URL.
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    # The low-level seam re-derives the plan, validates the consent digest, and
    # only then gates on prerequisites — so the stub returns a matching digest and
    # the call passes a Codex context plus a plan whose digest agrees.
    monkeypatch.setattr(
        doctor,
        "codex_setup_plan",
        lambda *a, **k: {
            "digest": "d0",
            "prerequisites": {"bash": True, "git": False, "cygpath": False},
        },
    )
    codex = doctor.HostContext.from_host(doctor.Host.CODEX)
    with pytest.raises(doctor.ChinamaxError) as exc:
        doctor.apply_codex_setup({"digest": "d0", "workspace": None}, "d0", context=codex)
    assert doctor._GIT_FOR_WINDOWS_URL in str(exc.value)


# ── Step 1: _find_conda resolution ──────────────────────────────────────────


def test_find_conda_precedence_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "_find_conda", _REAL_FIND_CONDA)  # undo the autouse stub
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    home = tmp_path / "h"
    (home / "miniconda3" / "bin").mkdir(parents=True)
    conda = home / "miniconda3" / "bin" / "conda"
    conda.write_text("#!/bin/sh\n")
    os.chmod(conda, stat.S_IRWXU)
    monkeypatch.setenv("HOME", str(home))
    # ~/miniconda3 beats a PATH conda.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/conda")
    assert doctor._find_conda() == str(conda)
    # Remove the local conda -> PATH fallback.
    conda.unlink()
    assert doctor._find_conda() == "/usr/bin/conda"
    # Neither -> None.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor._find_conda() is None


def test_find_conda_precedence_win32(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "_find_conda", _REAL_FIND_CONDA)
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    home = tmp_path / "winhome"
    scripts = home / "miniconda3" / "Scripts"
    scripts.mkdir(parents=True)
    # _is_executable(win32) needs a real .exe/.bat file, not just a monkeypatch.
    conda_exe = scripts / "conda.exe"
    conda_exe.write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: r"C:\other\conda.exe")
    assert doctor._find_conda() == str(conda_exe)
    # Scripts/conda.exe gone -> condabin/conda.bat is the second candidate.
    conda_exe.unlink()
    condabin = home / "miniconda3" / "condabin"
    condabin.mkdir()
    conda_bat = condabin / "conda.bat"
    conda_bat.write_text("")
    assert doctor._find_conda() == str(conda_bat)
    # Both gone -> PATH fallback (shutil.which, returned as-is).
    conda_bat.unlink()
    assert doctor._find_conda() == r"C:\other\conda.exe"


def test_find_env_python_keeps_path_conda_fallback(monkeypatch, tmp_path):
    """The step-1 non-collapse guard: ~/miniconda3 resolves _find_conda but its env
    probe fails, while a PATH conda resolves the env -> the env is still found. And
    _find_conda is NOT memoized: a second call under a different HOME re-resolves."""
    monkeypatch.setattr(doctor, "_find_conda", _REAL_FIND_CONDA)
    monkeypatch.setattr(doctor.sys, "platform", "linux")

    home = tmp_path / "h"
    (home / "miniconda3" / "bin").mkdir(parents=True)
    local_conda = home / "miniconda3" / "bin" / "conda"
    local_conda.write_text("#!/bin/sh\n")
    os.chmod(local_conda, stat.S_IRWXU)
    monkeypatch.setenv("HOME", str(home))
    env_python = _fake_python(tmp_path, name="envpy")

    monkeypatch.setattr(
        doctor.shutil, "which", lambda name: "/usr/bin/conda" if name == "conda" else None
    )
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[0])
        ok = cmd[0] == "conda"  # the local ~/miniconda3 conda fails; PATH conda wins
        return types.SimpleNamespace(
            returncode=0 if ok else 1, stdout=(env_python + "\n") if ok else "", stderr=""
        )

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert doctor._find_env_python() == env_python
    # The absolute ~/miniconda3 conda was tried FIRST (prepended), then bare conda.
    assert seen == [str(local_conda), "conda"]

    # Not memoized: a second call under a different HOME resolves a different conda.
    home2 = tmp_path / "h2"
    (home2 / "miniconda3" / "bin").mkdir(parents=True)
    local_conda2 = home2 / "miniconda3" / "bin" / "conda"
    local_conda2.write_text("#!/bin/sh\n")
    os.chmod(local_conda2, stat.S_IRWXU)
    monkeypatch.setenv("HOME", str(home2))
    env_python2 = _fake_python(tmp_path, name="envpy2")
    seen.clear()

    def fake_run2(cmd, **kwargs):
        seen.append(cmd[0])
        ok = cmd[0] == str(local_conda2)
        return types.SimpleNamespace(
            returncode=0 if ok else 1, stdout=(env_python2 + "\n") if ok else "", stderr=""
        )

    monkeypatch.setattr(doctor.subprocess, "run", fake_run2)
    assert doctor._find_env_python() == env_python2
    assert seen[0] == str(local_conda2)  # re-resolved to home2's conda, not a cache


def test_create_env_uses_resolved_conda(monkeypatch):
    monkeypatch.setattr(doctor, "_find_conda", lambda: "/abs/miniconda3/bin/conda")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    ok, detail = doctor._create_env()
    assert ok and detail == ""
    assert seen["cmd"] == [
        "/abs/miniconda3/bin/conda", "create", "-y", "-n", "chinamax", "python=3.12",
    ]


# ── Step 3: Rectification command emission pins (no rm/del cleanup line) ─────


def test_emission_linux_x86_64(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        doctor.shutil, "which", lambda n: "/usr/bin/apt-get" if n == "apt-get" else None
    )
    rows = doctor.prerequisite_fixes({"bash": False, "miniconda": False})
    assert [r["name"] for r in rows] == ["bash", "miniconda"]  # bash before miniconda
    bash_row, mini = rows
    assert bash_row["commands"] == ["sudo apt-get install -y bash"]
    assert bash_row["run_policy"] == "privileged" and bash_row["shell"] == "bash"
    assert bash_row["install_location"] == "system package manager"
    assert mini["commands"] == [
        'curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh '
        '-o "$HOME/.chinamax-miniconda.sh"',
        'bash "$HOME/.chinamax-miniconda.sh" -b -u -p "$HOME/miniconda3"',
        '"$HOME/miniconda3/bin/conda" init bash',
    ]
    assert mini["run_policy"] == "agent" and mini["shell"] == "bash"
    assert mini["install_location"] == "$HOME/miniconda3"
    # Operator override: exactly 3 commands, no rm cleanup line, no deletion warning.
    assert len(mini["commands"]) == 3
    assert not any(c.startswith("rm ") for c in mini["commands"])
    assert "rm " not in mini["summary"] and "delete" not in mini["summary"].lower()


def test_emission_linux_aarch64(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)  # no package manager
    rows = doctor.prerequisite_fixes({"bash": False, "miniconda": False})
    bash_row, mini = rows
    assert "Miniconda3-latest-Linux-aarch64.sh" in mini["commands"][0]
    assert "-b -u -p" in mini["commands"][1]
    # No package manager -> advice-only bash row.
    assert bash_row["run_policy"] == "operator" and bash_row["commands"] == []


@pytest.mark.parametrize(
    "manager,command",
    [
        ("apt-get", "sudo apt-get install -y bash"),
        ("dnf", "sudo dnf install -y bash"),
        ("yum", "sudo yum install -y bash"),
        ("pacman", "sudo pacman -S --noconfirm bash"),
        ("zypper", "sudo zypper install -y bash"),
        ("apk", "sudo apk add bash"),
    ],
)
def test_emission_linux_package_managers(monkeypatch, manager, command):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(
        doctor.shutil, "which", lambda n: f"/usr/bin/{n}" if n == manager else None
    )
    rows = doctor.prerequisite_fixes({"bash": False})
    assert rows[0]["commands"] == [command]
    assert rows[0]["run_policy"] == "privileged" and rows[0]["shell"] == "bash"


def test_emission_darwin(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.setattr(
        doctor.shutil, "which", lambda n: "/opt/homebrew/bin/brew" if n == "brew" else None
    )
    monkeypatch.setattr(doctor.platform, "machine", lambda: "arm64")
    rows = doctor.prerequisite_fixes({"bash": False, "miniconda": False})
    assert [r["name"] for r in rows] == ["bash", "miniconda"]
    bash_row, mini = rows
    assert bash_row["commands"] == ["brew install bash"] and bash_row["run_policy"] == "agent"
    assert bash_row["install_location"] == "Homebrew"
    assert "Miniconda3-latest-MacOSX-arm64.sh" in mini["commands"][0]
    assert mini["commands"][2] == '"$HOME/miniconda3/bin/conda" init bash zsh'
    assert len(mini["commands"]) == 3  # no rm cleanup line
    # x86_64 installer name.
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
    rows = doctor.prerequisite_fixes({"miniconda": False})
    assert "Miniconda3-latest-MacOSX-x86_64.sh" in rows[0]["commands"][0]
    # brew absent -> advice-only bash row.
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    rows = doctor.prerequisite_fixes({"bash": False})
    assert rows[0]["run_policy"] == "operator" and rows[0]["commands"] == []
    assert "brew install bash" in rows[0]["summary"] and "Homebrew" in rows[0]["summary"]


def test_emission_win32(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    # winget present.
    monkeypatch.setattr(
        doctor.shutil, "which", lambda n: r"C:\winget.exe" if n == "winget" else None
    )
    rows = doctor.prerequisite_fixes(
        {"git": False, "bash": False, "cygpath": False, "miniconda": False}
    )
    assert [r["name"] for r in rows] == ["Git for Windows", "miniconda"]  # git before miniconda
    git_row, mini = rows
    assert git_row["missing_tools"] == ["git", "bash", "cygpath"]
    assert git_row["commands"] == [
        "winget install --id Git.Git -e --silent "
        "--accept-source-agreements --accept-package-agreements"
    ]
    assert git_row["run_policy"] == "agent" and git_row["shell"] == "native"
    assert git_row["install_location"] == r"Program Files\Git"
    assert mini["shell"] == "cmd" and mini["install_location"] == r"%USERPROFILE%\miniconda3"
    assert mini["commands"] == [
        r'curl.exe -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe '
        r'-o "%TEMP%\chinamax-miniconda.exe"',
        r'start /wait "" "%TEMP%\chinamax-miniconda.exe" /InstallationType=JustMe '
        r'/RegisterPython=0 /AddToPath=0 /S /D=%USERPROFILE%\miniconda3',
        r'"%USERPROFILE%\miniconda3\Scripts\conda.exe" init cmd.exe powershell bash',
    ]
    # Operator override: exactly 3 commands, no del cleanup line.
    assert len(mini["commands"]) == 3
    assert not any(c.startswith("del ") for c in mini["commands"])
    # winget absent -> fail-loud PowerShell fallback naming the manual URL.
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    rows = doctor.prerequisite_fixes({"git": False, "bash": False, "cygpath": False})
    git_row = rows[0]
    assert git_row["shell"] == "powershell"
    assert git_row["install_location"] == r"%LocalAppData%\Programs\Git"
    assert doctor._GIT_FOR_WINDOWS_URL in git_row["summary"]
    cmd = git_row["commands"][0]
    assert cmd.startswith("powershell -NoProfile -Command")
    assert "$ErrorActionPreference='Stop'" in cmd
    assert "-PassThru" in cmd and "exit $p.ExitCode" in cmd


@pytest.mark.parametrize(
    "plat,machine",
    [
        ("linux", "ppc64le"),
        ("linux", "s390x"),
        ("linux", "armv7l"),
        ("darwin", "ppc"),
    ],
)
def test_emission_unsupported_arch_is_advice_only(monkeypatch, plat, machine):
    monkeypatch.setattr(doctor.sys, "platform", plat)
    monkeypatch.setattr(doctor.platform, "machine", lambda: machine)
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    rows = doctor.prerequisite_fixes({"miniconda": False})
    assert rows[0]["name"] == "miniconda"
    assert rows[0]["commands"] == [] and rows[0]["run_policy"] == "operator"
    # Names the base URL for a manual install, never a 404-bound installer filename.
    assert "repo.anaconda.com/miniconda/" in rows[0]["summary"]
    assert "Miniconda3-latest" not in rows[0]["summary"]


# ── Step 4: Phase-A pause and Codex digest binding ──────────────────────────


def test_run_setup_phase_a_pauses_on_missing_prerequisite(
    tmp_path, keyless_home, isolated, monkeypatch, capsys
):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        doctor.shutil, "which", lambda n: "/usr/bin/bash" if n == "bash" else None
    )
    # Miniconda MISSING (override the autouse present-stub).
    monkeypatch.setattr(doctor, "_find_conda", lambda: None)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    resolved = _fake_python(tmp_path)
    fixers = _Fixers()
    probed = {"deps": 0}

    def check_deps(_python):
        probed["deps"] += 1
        return {name: True for name in doctor.DEPS}

    code = doctor.run_setup(
        _ns(True, workspace),
        find_env_python=lambda: resolved,
        check_deps=check_deps,
        create_env=fixers.create_env,
        install_deps=fixers.install_deps,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert report["fixes"] == []
    assert report["prerequisites"] == {"bash": True, "miniconda": False}
    assert any(r["name"] == "miniconda" for r in report["prerequisite_fixes"])
    # JSON carries the exact commands.
    assert any(
        "Miniconda3-latest-Linux-x86_64.sh" in c
        for r in report["prerequisite_fixes"]
        for c in r["commands"]
    )
    # No injected FIXER ran, but diagnose's dep probe DID (Phase A is not
    # side-effect-free): the interpreter was recorded.
    assert fixers.creates == 0 and fixers.installs == []
    assert probed["deps"] >= 1
    assert doctor.python_path_file().read_text(encoding="utf-8").strip() == resolved

    # The human render carries the rectification commands too.
    doctor.run_setup(
        _ns(False, workspace),
        find_env_python=lambda: resolved,
        check_deps=check_deps,
        create_env=fixers.create_env,
        install_deps=fixers.install_deps,
    )
    human = capsys.readouterr().out
    assert "rectification commands" in human
    assert "Miniconda3-latest-Linux-x86_64.sh" in human
    assert fixers.creates == 0 and fixers.installs == []


def test_codex_setup_plan_binds_prerequisite_fixes(monkeypatch, keyless_home, isolated):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor.platform, "machine", lambda: "x86_64")
    # All prerequisites missing, held fixed so ONLY the rows vary (winget present
    # vs absent) while `prerequisites` itself is unchanged.
    monkeypatch.setattr(
        doctor,
        "prerequisite_status",
        lambda: {"git": False, "bash": False, "cygpath": False, "miniconda": False},
    )
    codex = doctor.HostContext.from_host(doctor.Host.CODEX)
    injected = dict(
        context=codex,
        find_env_python=lambda: None,
        check_deps=lambda _p: {n: False for n in doctor.DEPS},
    )

    monkeypatch.setattr(
        doctor.shutil, "which", lambda n: r"C:\winget.exe" if n == "winget" else None
    )
    plan_present = doctor.codex_setup_plan(None, **injected)

    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)  # winget absent
    plan_absent = doctor.codex_setup_plan(None, **injected)

    # prerequisites unchanged, rows differ, so the consent digest differs.
    assert plan_present["prerequisites"] == plan_absent["prerequisites"]
    assert plan_present["prerequisite_fixes"] != plan_absent["prerequisite_fixes"]
    assert plan_present["digest"] != plan_absent["digest"]
    # The rows live INSIDE the digested structure (digest placement is load-bearing).
    assert plan_present["digest_input"]["prerequisite_fixes"] == plan_present["prerequisite_fixes"]
