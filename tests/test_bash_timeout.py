"""A bash timeout is an observation, not a Job failure — and it kills the whole group."""

from __future__ import annotations

import io
import os
import time

import pytest

from chinamax.spec import DEFAULT_BASH_TIMEOUT_S, parse_spec
from conftest import REPORT_PAYLOAD, bash_script

#: Backgrounds a descendant, records its pid, prints, then hangs past the timeout.
HANGING_COMMAND = "sleep 300 & echo $! > child.pid; echo working; sleep 5"

#: Every way `bash_timeout_s` can be wrong. `true` is here because bool
#: subclasses int, so a naive isinstance check reads it as a 1 s timeout.
INVALID_TIMEOUTS = [
    pytest.param(0, id="zero"),
    pytest.param(-5, id="negative"),
    pytest.param("soon", id="non_numeric_string"),
    pytest.param(True, id="boolean"),
    pytest.param(float("inf"), id="non_finite"),
]


def wait_until_gone(pid: int, deadline_s: float = 5.0) -> bool:
    """Report whether a pid is gone, allowing for the moment reaping takes."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.05)
    return False


def test_timeout_observation_continues(job_env):
    """The timeout reports what it captured, kills the group, and the loop goes on."""
    env = job_env(bash_script(HANGING_COMMAND))

    started = time.monotonic()
    assert env.run(env.spec(bash_timeout_s=1)) == 0
    elapsed = time.monotonic() - started

    # The discriminator, and the reason "the descendant is dead" is not enough on
    # its own: the orphan inherits the pipes, so killing only the child still ends
    # with it dead and the assertions below green — 300 s later, once `sleep 300`
    # exits on its own and the readers finally see EOF. Only the group kill gets
    # here in about a second, and ADR 0002 leaves the loop no other way out.
    assert elapsed < 30, (
        f"the job took {elapsed:.1f}s: the command's process group outlived its "
        "timeout and run_bash blocked until the descendant exited by itself"
    )

    observation = env.observations()[0]
    assert "TIMED OUT" in observation["content"]
    assert "working" in observation["content"]
    # The Runtime itself survived and ran the next scripted turn.
    assert env.result() == REPORT_PAYLOAD
    assert len(env.requests) == 2

    descendant = int((env.workspace / "child.pid").read_text(encoding="utf-8").strip())
    assert wait_until_gone(descendant), f"backgrounded descendant {descendant} outlived its group"


@pytest.mark.parametrize("value", INVALID_TIMEOUTS)
def test_invalid_timeout_rejected(job_env, capsys, value):
    """A bad timeout fails spec validation rather than killing every command."""
    env = job_env(bash_script("echo hi"))

    assert env.run(env.spec(bash_timeout_s=value)) != 0

    assert "bash_timeout_s" in capsys.readouterr().err
    assert env.requests == []
    # Omitting the field resolves to the documented default instead.
    assert parse_spec(env.spec()).bash_timeout_s == DEFAULT_BASH_TIMEOUT_S == 600.0


class _FakeProc:
    """A spawned command that produces no output and exits 0 immediately."""

    pid = 1234

    def __init__(self) -> None:
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _recording_popen(recorder: list[str]):
    """A ``subprocess.Popen`` stand-in that records the spawned executable."""

    def popen(argv, **_kwargs):
        recorder.append(argv[0])
        return _FakeProc()

    return popen


def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("windows_tool_path must not be consulted off win32")


def test_win32_bash_spawn_uses_resolved_git_bash(monkeypatch, tmp_path):
    """On Windows the bash tool spawns the resolver's Git Bash, not bare ``bash``.

    Guards the divergence bug: setup probed the Git for Windows install tree while
    the spawn relied on PATH, so a default install (bash OFF PATH) spawned nothing.
    """
    from chinamax import state
    from chinamax.confinement import ToolContext
    from chinamax.tools import bash

    context = ToolContext(root=tmp_path, write=True, bash_timeout_s=5.0)
    monkeypatch.setattr(bash.sys, "platform", "win32")
    # The win32 branch reads a Windows-only creationflag absent on the Linux runner.
    monkeypatch.setattr(
        bash.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )
    monkeypatch.setattr(state, "read_pid_start_time", lambda _pid: None)

    recorder: list[str] = []
    monkeypatch.setattr(bash.subprocess, "Popen", _recording_popen(recorder))

    sentinel = r"C:\Git\bin\bash.exe"
    monkeypatch.setattr(state, "windows_tool_path", lambda _name: sentinel)
    result = bash.run_bash("echo hi", context)
    assert recorder == [sentinel]
    assert result["exit_code"] == 0 and result["timed_out"] is False

    # No Git for Windows root resolves → fall back to bare "bash".
    monkeypatch.setattr(state, "windows_tool_path", lambda _name: None)
    recorder.clear()
    bash.run_bash("echo hi", context)
    assert recorder == ["bash"]


def test_non_win32_bash_spawn_uses_bare_bash(monkeypatch, tmp_path):
    """Off Windows the spawn stays bare ``bash`` and never consults the resolver."""
    from chinamax import state
    from chinamax.confinement import ToolContext
    from chinamax.tools import bash

    context = ToolContext(root=tmp_path, write=True, bash_timeout_s=5.0)
    monkeypatch.setattr(bash.sys, "platform", "linux")
    monkeypatch.setattr(state, "windows_tool_path", _fail_if_called)

    recorder: list[str] = []
    monkeypatch.setattr(bash.subprocess, "Popen", _recording_popen(recorder))
    bash.run_bash("echo hi", context)
    assert recorder == ["bash"]
