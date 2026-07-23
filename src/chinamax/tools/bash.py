"""The bash tool: a cwd-pinned shell command with bounded output capture.

Denylist, timeouts and read-only enforcement arrive in a later slice; the
cwd-pinning and the bounded capture exist now, because with no command timeout
yet a runaway command must not be able to exhaust Runtime memory.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import BinaryIO

MAX_CAPTURE_BYTES = 50_000
_READ_SIZE = 4096

BASH_TOOL = {
    "name": "bash",
    "description": (
        "Run a shell command with bash. The command runs with the workspace as its "
        "working directory. Returns the exit code with the captured stdout and stderr."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


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
        return f"[... truncated to the last {self._limit} bytes ...]\n{body}"


def run_bash(command: str, workspace: Path) -> dict:
    """Run one shell command pinned to the workspace.

    Args:
        command: The shell command, run via ``bash -c``.
        workspace: The working directory the command is pinned to.

    Returns:
        ``{"exit_code", "stdout", "stderr"}``, each stream bounded to its tail.
    """
    process = subprocess.Popen(
        ["bash", "-c", command],
        cwd=str(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    exit_code = process.wait()
    for reader in readers:
        reader.join()
    return {"exit_code": exit_code, "stdout": stdout.text(), "stderr": stderr.text()}


def format_bash_result(result: dict) -> str:
    """Render a bash result so both streams and the exit code are inspectable."""
    return (
        f"exit_code: {result['exit_code']}\n"
        f"stdout:\n{result['stdout']}\n"
        f"stderr:\n{result['stderr']}"
    )


def _drain(pipe: BinaryIO, buffer: TailBuffer) -> None:
    """Read a pipe to EOF into a bounded buffer."""
    with pipe:
        for chunk in iter(lambda: pipe.read(_READ_SIZE), b""):
            buffer.feed(chunk)
