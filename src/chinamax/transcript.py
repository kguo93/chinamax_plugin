"""The Job's Thread transcript: a versioned, write-ahead JSONL record.

Every line is an object carrying ``v`` (schema version), ``ts`` and ``kind``.
``kind: "message"`` records carry ``role``/``content`` and ARE the replayable
conversation; any other kind is metadata a replay skips. This slice emits only
``message`` records — the discriminator exists so later slices can add
non-replayable events without a format break.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


class Transcript:
    """Write-ahead JSONL writer for one Job's Thread.

    An existing transcript is truncated: a fresh run never grafts a new
    conversation onto stale history.
    """

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
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
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "message",
                "role": role,
                "content": content,
            }
        )

    def close(self) -> None:
        """Close the underlying file."""
        self._handle.close()

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
