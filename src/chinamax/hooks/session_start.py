"""SessionStart hook: inject the running/recent Job digest, export provenance.

Writes the digest to STDOUT (Claude Code injects it as session context) and
appends env exports to ``$CLAUDE_ENV_FILE`` when set. The digest is BOUNDED: every
active or interrupted Job plus the 5 most recent finished ones within 24 h,
capped at ~2 KB with a trailing ``(+N more)`` — inherited work a fresh session
must learn about (ADR 0004) without flooding the context.
"""

from __future__ import annotations

import os
import shlex
import sys
import time

from chinamax import state
from chinamax.hooks import read_event, resolve_workspace

#: Exports appended for a dispatched Bash to inherit. ``CHINAMAX_SESSION_ID`` is
#: provenance; ``CLAUDE_PLUGIN_DATA`` is load-bearing — the hooks and the Bridge's
#: dispatches must agree on the state root, or Jobs land in one root while the
#: digest reads the other.
SESSION_ID_EXPORT = "CHINAMAX_SESSION_ID"
PLUGIN_DATA_EXPORT = "CLAUDE_PLUGIN_DATA"

#: The digest cap, and the room reserved inside it for a trailing ``(+N more)``.
DIGEST_CAP_BYTES = 2048
_MORE_RESERVE_BYTES = 24
#: How many finished Jobs the digest lists, and the recency window for them.
FINISHED_LIMIT = 5
FINISHED_WINDOW_S = 24 * 3600


def main() -> int:
    """Emit the digest and the env exports, degrading to a clean exit.

    Returns:
        0 always. A whole-hook failure leaves stdout empty and sends diagnostics
        to stderr, never a traceback into Claude's context.
    """
    event: dict = {}
    try:
        event = read_event()
        digest = _build_digest(event)
    except Exception as error:  # noqa: BLE001 - never fail the session
        print(
            f"chinamax session_start hook: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        digest = ""
    if digest:
        sys.stdout.write(digest)
    try:
        _export_env(event)
    except Exception as error:  # noqa: BLE001 - exports are best-effort
        print(f"chinamax session_start hook (env): {error}", file=sys.stderr)
    return 0


def _build_digest(event: dict) -> str:
    """Render the bounded digest for this workspace, or "" when there is nothing."""
    root = resolve_workspace(event)
    if root is None:
        return ""
    records, _ = state.list_jobs_tolerant(root)

    now = time.time()
    active, interrupted, finished = [], [], []
    for record in records:
        effective = state.effective_status(record)
        if effective in state.ACTIVE_STATUSES:
            active.append(record)
        elif effective == state.STATUS_INTERRUPTED:
            interrupted.append(record)
        elif effective in state.FINISHED_STATUSES and _within_window(record, now):
            finished.append(record)

    if not (active or interrupted or finished):
        return ""

    finished.sort(key=state.latest_key, reverse=True)
    shown_finished = finished[:FINISHED_LIMIT]
    hidden = len(finished) - len(shown_finished)
    ordered = active + interrupted + shown_finished

    header = f"chinamax Jobs in {root.name or 'workspace'}:"
    body, dropped = _pack(header, [_line(record) for record in ordered])
    hidden += dropped

    rendered = "\n".join([header, *body])
    if hidden:
        rendered += f"\n  (+{hidden} more)"
    return rendered + "\n"


def _within_window(record: dict, now: float) -> bool:
    """Report whether a finished Job's terminal time is within the 24 h window."""
    terminal = state.parse_timestamp(record.get("completedAt")) or state.parse_timestamp(
        record.get("updatedAt")
    )
    return terminal is not None and now - terminal <= FINISHED_WINDOW_S


def _line(record: dict) -> str:
    """Render one Job's digest line: id, status, phase, elapsed, profile, title."""
    return (
        f"  {record['id']}  {state.effective_status(record):<11}  "
        f"{(record.get('phase') or '-'):<14}  {state.elapsed(record):>8}  "
        f"{record.get('profile') or '-'}  {record.get('title') or ''}"
    ).rstrip()


def _pack(header: str, lines: list[str]) -> tuple[list[str], int]:
    """Keep as many lines as fit the cap; return the kept lines and the drop count."""
    size = len(header) + 1
    kept: list[str] = []
    for index, line in enumerate(lines):
        projected = size + len(line) + 1
        if projected > DIGEST_CAP_BYTES - _MORE_RESERVE_BYTES:
            return kept, len(lines) - index
        kept.append(line)
        size = projected
    return kept, 0


def _export_env(event: dict) -> None:
    """Append the shell-quoted env exports to ``$CLAUDE_ENV_FILE`` when set."""
    env_file = os.environ.get("CLAUDE_ENV_FILE", "").strip()
    if not env_file:
        return
    exports: list[tuple[str, str]] = []
    session = event.get("session_id")
    if session:
        exports.append((SESSION_ID_EXPORT, str(session)))
    plugin_data = os.environ.get(PLUGIN_DATA_EXPORT, "")
    if plugin_data:
        exports.append((PLUGIN_DATA_EXPORT, plugin_data))
    if not exports:
        return
    with open(env_file, "a", encoding="utf-8") as handle:
        for name, value in exports:
            handle.write(f"export {name}={shlex.quote(value)}\n")


if __name__ == "__main__":
    sys.exit(main())
