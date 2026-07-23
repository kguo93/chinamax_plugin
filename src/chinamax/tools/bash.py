"""The bash tool: a cwd-pinned shell command, denylisted and time-bounded.

Two bounds apply, and they are not interchangeable. `TailBuffer` bounds the
capture WHILE it streams, so a command producing gigabytes cannot exhaust
Runtime memory before its timeout arrives; `truncate_tail` bounds a finished
string, and the registry applies it to every tool's observation. Both share one
limit and one notice, so a truncated observation reads the same wherever it came
from.

The timeout kills the child's whole process group, never the Runtime's own — a
`sleep &` the command backgrounded must die with it. A descendant that calls
`setsid()` itself still escapes; ADR 0005 documents that rather than chasing it.
Note what that residual actually costs: an escaped descendant inherits the pipes,
so it does not merely leak a process — it holds `run_bash` open until it exits on
its own, and ADR 0002 gives the loop no wall-clock rescue to cut that short.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import BinaryIO

from chinamax import ToolError
from chinamax.confinement import ToolContext, denied_reason, write_shaped_reason

MAX_CAPTURE_BYTES = 50_000
#: Seconds between the SIGTERM a timeout sends and the SIGKILL that follows.
TERMINATE_GRACE_S = 5.0
_READ_SIZE = 4096
_TRUNCATION_NOTICE = "[... truncated to the last {limit} bytes ...]\n"

BASH_TOOL = {
    "name": "bash",
    "description": (
        "Run a shell command with bash. The command runs with the workspace as its "
        "working directory. Returns the exit code with the captured stdout and stderr. "
        "Commands that destroy data or leave the machine (rm, dd, git push, sudo, "
        "shutdown, piping a download into a shell) are refused."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


def truncate_tail(text: str, limit: int = MAX_CAPTURE_BYTES) -> str:
    """Bound a finished string to its last ``limit`` bytes, noting any loss."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return _TRUNCATION_NOTICE.format(limit=limit) + encoded[-limit:].decode(
        "utf-8", errors="replace"
    )


class TailBuffer:
    """A bounded tail of a byte stream, filled as output arrives."""

    def __init__(self, limit: int = MAX_CAPTURE_BYTES) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        """Append a chunk, dropping the oldest bytes past the limit."""
        self._buffer += chunk
        overflow = len(self._buffer) - self._limit
        if overflow > 0:
            del self._buffer[:overflow]
            self._truncated = True

    def text(self) -> str:
        """Decode the tail, prefixed with a notice when bytes were dropped."""
        body = self._buffer.decode("utf-8", errors="replace")
        if not self._truncated:
            return body
        return _TRUNCATION_NOTICE.format(limit=self._limit) + body


class BashTool:
    """Run one shell command, refusing the denied and the write-shaped."""

    spec = BASH_TOOL
    writes = False

    def execute(self, value: dict, context: ToolContext) -> str:
        """Check the command against policy, then run it pinned to the workspace.

        Args:
            value: The validated tool input, carrying ``command``.
            context: The Job's tool context.

        Returns:
            The rendered observation.

        Raises:
            ToolError: If the denylist, or a read-only Job, refuses the command.
        """
        command = value["command"]
        reason = denied_reason(command)
        if reason is None and not context.write:
            reason = write_shaped_reason(command)
        if reason is not None:
            raise ToolError(reason)
        return format_bash_result(run_bash(command, context))


def run_bash(command: str, context: ToolContext) -> dict:
    """Run one shell command pinned to the workspace, bounded by its timeout.

    Args:
        command: The shell command, run via ``bash -c``.
        context: The Job's tool context (workspace root and bash timeout).

    Returns:
        ``{"exit_code", "stdout", "stderr", "timed_out"}``. A timed-out command
        has no exit code — it was killed — so ``exit_code`` is None.
    """
    process = subprocess.Popen(
        ["bash", "-c", command],
        cwd=str(context.root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Its own process group, so expiry kills the command's descendants and
        # not the Runtime that is supervising it.
        start_new_session=True,
    )
    stdout, stderr = TailBuffer(), TailBuffer()
    # Both pipes must be drained concurrently: reading one to completion first
    # deadlocks as soon as the other fills its OS buffer.
    readers = [
        threading.Thread(target=_drain, args=(process.stdout, stdout)),
        threading.Thread(target=_drain, args=(process.stderr, stderr)),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        exit_code = process.wait(timeout=context.bash_timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        _kill_group(process)
    # Joined only after the group is dead: a backgrounded descendant inherits
    # the pipes, so the readers see EOF only once the whole group is gone.
    for reader in readers:
        reader.join()
    return {
        "exit_code": exit_code,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "timed_out": timed_out,
    }


def format_bash_result(result: dict) -> str:
    """Render a bash result so both streams and the outcome are inspectable."""
    if result["timed_out"]:
        outcome = (
            "TIMED OUT: the command exceeded its time limit and its process group "
            "was killed, so it has no exit code. The output captured before the "
            "timeout follows."
        )
    else:
        outcome = f"exit_code: {result['exit_code']}"
    return f"{outcome}\nstdout:\n{result['stdout']}\nstderr:\n{result['stderr']}"


def _kill_group(process: subprocess.Popen) -> None:
    """Kill a timed-out command's whole process group: SIGTERM, grace, SIGKILL."""
    group = os.getpgid(process.pid)
    _signal_group(group, signal.SIGTERM)
    try:
        process.wait(timeout=TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(group, signal.SIGKILL)
    process.wait()


def _signal_group(group: int, number: int) -> None:
    """Signal a process group, tolerating one that has already exited."""
    try:
        os.killpg(group, number)
    except ProcessLookupError:
        pass


def _drain(pipe: BinaryIO, buffer: TailBuffer) -> None:
    """Read a pipe to EOF into a bounded buffer."""
    with pipe:
        for chunk in iter(lambda: pipe.read(_READ_SIZE), b""):
            buffer.feed(chunk)
