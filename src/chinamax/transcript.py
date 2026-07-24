"""The Job's Thread transcript: a versioned, write-ahead JSONL record.

Every line is an object carrying ``v`` (schema version), ``ts`` and ``kind``.
``kind: "message"`` records carry ``role``/``content`` and ARE the replayable
conversation; any other kind is metadata a replay skips. ``kind: "retry"`` is
the first such metadata record: a retried attempt appends nothing to the
canonical history, so a retry can never leave a phantom turn behind.

Reading one back for a resume or a relaunch REPAIRS its tail rather than
truncating it: a crash leaves a legitimate intermediate state, and every turn
before it is history the continuation needs.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

SCHEMA_VERSION = 1

#: What a synthetic ``tool_result`` says when it answers a `tool_use` the worker
#: never got to. Two texts, because one of them is not a failure: a normally
#: completed Job ALWAYS ends on an unanswered `report_result`, and telling a
#: resuming model its own finished report was interrupted would be a lie.
INTERRUPTED_RESULT = (
    "[chinamax] No result was recorded for this tool call: the worker was "
    "interrupted before it completed."
)
REPORTED_RESULT = (
    "[chinamax] Result recorded. The previous job ended on this call and is "
    "being continued below."
)
_REPORT_RESULT = "report_result"


class Transcript:
    """Write-ahead JSONL writer for one Job's Thread.

    An existing transcript is truncated unless ``append`` is set: a fresh run
    never grafts a new conversation onto stale history, while a seeded run —
    a resume, or a relaunch on a crashed Job — continues the Thread it was
    handed rather than deleting it.
    """

    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], float] = time.time,
        append: bool = False,
    ) -> None:
        """Open a Job's Thread for writing.

        Args:
            path: The transcript path; its parent is created if absent.
            clock: Returns epoch seconds for record timestamps. Injected so a
                test can simulate a long wall clock — nothing compares these
                timestamps against a limit (ADR 0002).
            append: Keep an existing transcript and append to it, rather than
                truncating it.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._clock = clock
        self._handle = path.open("a" if append else "w", encoding="utf-8")

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


def read_repaired_messages(path: str | Path) -> list[dict]:
    """Replay a Thread and REPAIR its tail, for a resume or a relaunch.

    The tolerant reader every seeding path shares. It repairs and never
    truncates: dropping turns would break the full prior history a resume is
    for. Three tail shapes exist and each has a defined repair — a torn trailing
    line is dropped by `read_messages` (a half-written append is not a turn), an
    assistant turn whose ``tool_use`` blocks were never answered is KEPT and
    answered synthetically, and a trailing user turn is left alone for
    `merge_follow_up` to fold the follow-up into.

    Args:
        path: The transcript path.

    Returns:
        A message sequence in which every ``tool_use`` is answered.
    """
    return repair_messages(read_messages(path))


def repair_messages(messages: list[dict]) -> list[dict]:
    """Answer every unanswered ``tool_use`` with a synthetic ``tool_result``.

    A normally completed Job ALWAYS ends this way: the loop terminates on the
    ``report_result`` tool_use without answering it, because that block IS the
    terminus. Dropping the turn would delete the terminal report from every
    ordinary resume — and leave the API with a ``tool_use`` nothing answers,
    which it rejects.

    Args:
        messages: The replayed message sequence.

    Returns:
        A new sequence; the input is not mutated.
    """
    answered = {
        block.get("tool_use_id")
        for message in messages
        for block in _blocks(message)
        if block.get("type") == "tool_result"
    }
    repaired: list[dict] = []
    pending: list[dict] = []
    for message in messages:
        if pending:
            if message.get("role") == "user":
                message = {**message, "content": [*pending, *_blocks(message)]}
            else:
                repaired.append({"role": "user", "content": pending})
            pending = []
        repaired.append(message)
        if message.get("role") != "assistant":
            continue
        pending = [
            _synthetic_result(block)
            for block in _blocks(message)
            if block.get("type") == "tool_use" and block.get("id") not in answered
        ]
    if pending:
        repaired.append({"role": "user", "content": pending})
    return repaired


def merge_follow_up(messages: list[dict], text: str) -> list[dict]:
    """Add a follow-up to a repaired history without doubling the user role.

    A repaired history routinely ends in a user turn — the synthetic results of
    the terminal ``report_result``, or the write-ahead delta of the call a
    SIGKILL landed during. The follow-up therefore joins that message as another
    text block rather than becoming a second consecutive user message: the
    Profiles target third-party Anthropic-compatible endpoints whose tolerance
    for consecutive same-role messages is not guaranteed.

    Args:
        messages: The repaired history.
        text: The follow-up prompt.

    Returns:
        A new sequence ending in exactly one user turn carrying ``text``.
    """
    block = {"type": "text", "text": text}
    if messages and messages[-1].get("role") == "user":
        last = messages[-1]
        return [*messages[:-1], {**last, "content": [*_blocks(last), block]}]
    return [*messages, {"role": "user", "content": [block]}]


def write_messages(path: str | Path, messages: list[dict]) -> None:
    """Write a normalized message sequence as a fresh Thread transcript.

    The dispatcher's half of a resume: the copy is written from the repaired
    replay, so the new Job's Thread is already the history its worker will send.

    Args:
        path: The new Job's transcript path.
        messages: The repaired, merged message sequence.
    """
    with Transcript(path) as thread:
        for message in messages:
            thread.append_message(message["role"], message["content"])


def _blocks(message: dict) -> list[dict]:
    """Return a message's content blocks, tolerating a non-list content."""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _synthetic_result(block: dict) -> dict:
    """Build the ``tool_result`` that answers one unanswered ``tool_use``."""
    reported = block.get("name") == _REPORT_RESULT
    return {
        "type": "tool_result",
        "tool_use_id": block.get("id"),
        "content": REPORTED_RESULT if reported else INTERRUPTED_RESULT,
    }
