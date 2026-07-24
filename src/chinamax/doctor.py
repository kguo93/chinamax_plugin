"""The `/chinamax:setup` environment doctor.

One pass diagnoses everything a first run needs: the ``chinamax`` conda env, the
three dependencies importable BY THE RESOLVED ENV PYTHON (never the interpreter
the doctor itself runs under — a bootstrap run on a fresh machine has ``chinamax``
on its own ``PYTHONPATH`` and would otherwise grade itself), the API-key entries
per Profile (present/missing by NAME — values never touch any stream), and the
state root's writability (both the root and the per-workspace dir it grades,
since jobs/01 shifts the root between ``$CLAUDE_PLUGIN_DATA/state`` and the XDG
fallback). It also records the resolved env python where the Bridge and the shims
read it first.

The env-location and import probes are injectable so the suite can drive the
doctor hermetically without resolving this machine's real conda env (ADR 0011).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from chinamax import ChinamaxError, profiles, state

#: The three dependencies the doctor grades, in report order. ``pytest`` is in
#: runtime/01's optional ``[test]`` extra, which is why the create advice below
#: installs with that extra: the doctor's own advice must satisfy its own check.
DEPS = ("chinamax", "anthropic", "pytest")

#: The env the plugin's Runtime runs in, and the interpreter the shims prefer.
ENV_NAME = "chinamax"
#: The single-line record the shims / Bridge read the resolved interpreter from.
PYTHON_PATH_FILENAME = "python-path"

#: Injectable probe types: resolve the env's absolute python (None when absent),
#: and report which of DEPS import under a given python.
EnvPythonFinder = Callable[[], "str | None"]
DepChecker = Callable[[str], "dict[str, bool]"]


def data_root() -> Path:
    """Return the plugin data root the interpreter record lives under.

    jobs/01's state-root rule MINUS the ``/state`` suffix: ``$CLAUDE_PLUGIN_DATA``
    when set (no ``/state``), else ``$XDG_STATE_HOME/chinamax`` (the XDG fallback,
    which equals the state root there), else ``~/.local/state/chinamax``. An empty
    or relative value counts as unset, exactly as `state.state_root` treats them.
    """
    plugin_data = state._absolute_env_dir("CLAUDE_PLUGIN_DATA")
    if plugin_data is not None:
        return plugin_data
    xdg = state._absolute_env_dir("XDG_STATE_HOME")
    if xdg is not None:
        return xdg / ENV_NAME
    return Path.home() / ".local" / "state" / ENV_NAME


def python_path_file() -> Path:
    """Return the path the resolved env python is recorded at."""
    return data_root() / PYTHON_PATH_FILENAME


def source_repo_path() -> str:
    """Discover the source repo the create advice should ``pip install -e``.

    The editable install's origin when it can be found — never a hardcoded author
    path, since the fresh machine the doctor exists for keeps the checkout
    somewhere else (PRD user story 14) — else ``${CLAUDE_PLUGIN_ROOT}``.
    """
    try:
        import chinamax

        candidate = Path(chinamax.__file__).resolve().parents[2]
        if (candidate / "pyproject.toml").is_file():
            return str(candidate)
    except (OSError, IndexError, ImportError):
        pass
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    return root or "<the chinamax repo>"


def diagnose(
    workspace: str | Path | None = None,
    *,
    find_env_python: EnvPythonFinder | None = None,
    check_deps: DepChecker | None = None,
    record: bool = True,
) -> dict:
    """Run one full diagnosis and return the pinned ``--json`` document.

    Args:
        workspace: The workspace whose per-workspace state dir is graded.
        find_env_python: Resolve the env's absolute python (None when absent);
            the production resolver by default.
        check_deps: Report which of DEPS import under a python; the production
            subprocess checker by default. Only ever called with a real path —
            when the env is absent every dep is reported missing WITHOUT running
            an import, so a bootstrap run never grades its own interpreter.
        record: Whether to (re-)record the resolved env python.

    Returns:
        The report document matching the pinned schema.
    """
    find_env_python = find_env_python or _find_env_python
    check_deps = check_deps or _check_deps

    env_python = find_env_python()
    env_present = env_python is not None
    deps = (
        check_deps(env_python)
        if env_present
        else {name: False for name in DEPS}
    )

    root = state.state_root()
    workspace_dir = _workspace_state_dir(root, workspace)
    writable = _state_writable(workspace_dir)

    profile_rows = _profile_rows()
    ok = env_present and all(deps[name] for name in DEPS) and writable

    if record and env_present:
        _maybe_record_python(env_python)

    return {
        "ok": ok,
        "python": env_python,
        "state_root": str(root),
        "workspace_state_dir": str(workspace_dir),
        "state_writable": writable,
        "env": {"present": env_present, "path": env_python},
        "deps": {name: deps[name] for name in DEPS},
        "profiles": profile_rows,
    }


def render_report(report: dict) -> str:
    """Render the diagnosis as one human-readable pass. NEVER prints a key value."""
    lines = [f"chinamax setup — {'OK' if report['ok'] else 'PROBLEMS FOUND'}"]

    env = report["env"]
    if env["present"]:
        lines.append(f"  conda env '{ENV_NAME}': present ({env['path']})")
    else:
        repo = source_repo_path()
        lines.append(f"  conda env '{ENV_NAME}': MISSING — create it with:")
        lines.append(f"      conda create -y -n {ENV_NAME} python=3.12")
        lines.append(f"      conda run -n {ENV_NAME} pip install -e '{repo}[test]'")

    lines.append(f"  dependencies (imported under the '{ENV_NAME}' env python):")
    for name in DEPS:
        lines.append(f"      {name}: {'ok' if report['deps'][name] else 'MISSING'}")

    lines.append(f"  API keys ({profiles.keys_path()}):")
    for row in report["profiles"]:
        lines.append(f"      {row['name']}: {row['key']} ({row['key_env']})")

    lines.append("  state directory:")
    lines.append(f"      root: {report['state_root']}")
    lines.append(f"      this workspace: {report['workspace_state_dir']}")
    lines.append(f"      writable: {'yes' if report['state_writable'] else 'NO'}")

    if env["present"]:
        lines.append(f"  interpreter recorded at: {python_path_file()}")
    return "\n".join(lines) + "\n"


def run_setup(
    args: argparse.Namespace,
    *,
    find_env_python: EnvPythonFinder | None = None,
    check_deps: DepChecker | None = None,
) -> int:
    """Run the doctor for the ``setup`` verb and return the exit code.

    Args:
        args: The parsed ``setup`` arguments (``workspace`` and ``json``).
        find_env_python: Injected env resolver (production default otherwise).
        check_deps: Injected dep checker (production default otherwise).

    Returns:
        0 when everything the plugin needs is in place, 1 otherwise. Key
        presence is reported but never fails the run — an unused Profile must not
        block setup.
    """
    report = diagnose(
        args.workspace,
        find_env_python=find_env_python,
        check_deps=check_deps,
    )
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_report(report))
    return 0 if report["ok"] else 1


def _workspace_state_dir(root: Path, workspace: str | Path | None) -> Path:
    """Return the per-workspace state dir under ``root``, or the root itself.

    An unresolvable workspace still yields a graded directory — the root — rather
    than a failed diagnosis, so setup reports on a fresh checkout with no repo.
    """
    try:
        return root / state.workspace_key(state.resolve_workspace_root(workspace))
    except ChinamaxError:
        return root


def _state_writable(directory: Path) -> bool:
    """Report whether a state directory can be created and written under."""
    try:
        state.make_dir(directory)
        probe = directory / ".chinamax-setup-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _profile_rows() -> list[dict]:
    """Report every Profile's key env-var name and PRESENT/MISSING — never a value."""
    resolved = profiles.load_profiles()
    keys = profiles.load_keys()
    rows = []
    for name in sorted(resolved):
        profile = resolved[name]
        present = bool(keys.get(profile.api_key_env, "").strip())
        rows.append(
            {
                "name": name,
                "key_env": profile.api_key_env,
                "key": "PRESENT" if present else "MISSING",
            }
        )
    return rows


def _maybe_record_python(env_python: str) -> None:
    """Record the resolved env python, re-recording only a stale entry.

    A stored path that still resolves to an executable is trusted (the Bridge and
    shims already read it); a missing or no-longer-resolving one is re-recorded.
    """
    target = python_path_file()
    stored = _read_recorded_python(target)
    if stored is not None and _is_executable(stored):
        return
    _write_python_path(target, env_python)


def _read_recorded_python(target: Path) -> str | None:
    """Return the recorded interpreter path, or None when there is none."""
    try:
        value = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _write_python_path(target: Path, value: str) -> None:
    """Record one absolute interpreter path atomically (tmp+rename)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _is_executable(path: str) -> bool:
    """Report whether a path is an absolute, existing, executable file."""
    return bool(path) and os.path.isabs(path) and os.path.isfile(path) and os.access(
        path, os.X_OK
    )


def _find_env_python() -> str | None:
    """Resolve the ``chinamax`` env's absolute python, or None when absent.

    Prefers the conventional miniconda path, then asks ``conda`` for the env's
    interpreter without hardcoding conda's own location; anything that is not an
    executable absolute path is treated as absent.
    """
    conventional = Path.home() / "miniconda3" / "envs" / ENV_NAME / "bin" / "python"
    if _is_executable(str(conventional)):
        return str(conventional)
    try:
        finished = subprocess.run(
            ["conda", "run", "-n", ENV_NAME, "python", "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = finished.stdout.strip().splitlines()
    resolved = output[-1].strip() if output else ""
    return resolved if finished.returncode == 0 and _is_executable(resolved) else None


def _check_deps(env_python: str) -> dict[str, bool]:
    """Report which of DEPS import under the resolved env python."""
    return {name: _can_import(env_python, name) for name in DEPS}


def _can_import(env_python: str, module: str) -> bool:
    """Report whether ``module`` imports under ``env_python``."""
    try:
        finished = subprocess.run(
            [env_python, "-c", f"import {module}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return finished.returncode == 0
