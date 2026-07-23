"""The Job's Thread transcript: a versioned, write-ahead JSONL record.

Every line is an object carrying ``v`` (schema version), ``ts`` and ``kind``.
``kind: "message"`` records carry ``role``/``content`` and ARE the replayable
conversation; any other kind is metadata a replay skips. ``kind: "retry"`` is
the first such metadata record: a retried attempt appends nothing to the
canonical history, so a retry can never leave a phantom turn behind.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

SCHEMA_VERSION = 1


class Transcript:
    """Write-ahead JSONL writer for one Job's Thread.

    An existing transcript is truncated: a fresh run never grafts a new
    conversation onto stale history.
    """

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time) -> None:
        """Open a Job's Thread for writing.

        Args:
            path: The transcript path; its parent is created if absent.
            clock: Returns epoch seconds for record timestamps. Injected so a
                test can simulate a long wall clock — nothing compares these
                timestamps against a limit (ADR 0002).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._clock = clock
        self._handle = path.open("w", encoding="utf-8")

    def __enter__(self) -> "Transcript":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def append_message(self, role: str, content: list[dict]) -> None:
        """Append one conversation turn and flush it to the OS.

        Args:
            role: ``"user"`` or ``"assistant"``.
            content: The turn's content blocks as plain JSON-serializable dicts.
        """
        self._append(
            {
                "v": SCHEMA_VERSION,
                "ts": self._now(),
                "kind": "message",
                "role": role,
                "content": content,
            }
        )

    def append_retry(self, details: dict) -> None:
        """Append one retry decision as a non-replayable metadata record.

        Deliberately not a ``message`` record: the attempt it describes appended
        nothing to the canonical history, and a replay that picked this up would
        reconstruct a conversation the provider never saw.

        Args:
            details: The decision's fields (attempt, classification,
                failure_kind, sleep_s).
        """
        self._append(
            {"v": SCHEMA_VERSION, "ts": self._now(), "kind": "retry", **details}
        )

    def close(self) -> None:
        """Close the underlying file."""
        self._handle.close()

    def _now(self) -> str:
        """Return the current timestamp, ISO-8601 UTC, from the injected clock."""
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()

    def _append(self, record: dict) -> None:
        self._handle.write(json.dumps(record) + "\n")
        self._handle.flush()


def read_messages(path: str | Path) -> list[dict]:
    """Replay a Thread into the message sequence a resume would re-send.

    Non-``message`` records are skipped, and a torn trailing line is tolerated:
    JSONL appends are not atomic, so a crash mid-write can leave one.

    Args:
        path: The transcript path.

    Returns:
        ``{"role", "content"}`` dicts in file order.
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    messages: list[dict] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        if record.get("kind") == "message":
            messages.append({"role": record["role"], "content": record["content"]})
    return messages
