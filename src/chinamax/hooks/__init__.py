"""Session-lifecycle and Bridge-enforcement hook entrypoints and shared helpers.

Every hook is a python entrypoint (unit-testable with crafted stdin JSON) run
through the plugin's shims: `session_start` (registry + orphan reap + digest),
`session_end` (session reap; ADR 0004, reversed 2026-07-30), `stop` (running-Job
notice), `user_prompt` (the live-Bridge roster into main), and `bridge_contract`
(the classification contract re-injected into the Bridge). They read Job state
through the ONE shared tolerant enumeration seam `state.list_jobs_tolerant`,
resolve "THIS workspace" the same three-rung way, and degrade to a clean exit
rather than ever putting a traceback in Claude's context.

SessionStart, Stop and UserPromptSubmit additionally run the shared
`sweep_stale_supervision` in-process (ADR 0003/0004, amended 2026-07-31): a Job
must not outlive its Bridge, so each reaps this session's Bridge-owned Jobs whose
long-poll heartbeat has aged out.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from chinamax import ChinamaxError, state


def read_event() -> dict:
    """Read the hook's stdin JSON, tolerating empty or invalid input.

    A hook receives ``{session_id, cwd, hook_event_name, transcript_path}`` on
    stdin. Empty, unreadable or non-object input yields ``{}`` rather than an
    error, so a malformed event can never fail the whole hook.

    Returns:
        The parsed event object, or ``{}``.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def resolve_workspace(event: dict) -> Path | None:
    """Resolve THIS workspace from a hook event, walking to the git toplevel.

    The three rungs, in order (Codex parity): the stdin ``cwd``, then
    ``CLAUDE_PROJECT_DIR``, then the process cwd. Each is handed to jobs/01's
    workspace-root resolver, which walks to ``git rev-parse --show-toplevel`` —
    so a session opened in a SUBDIRECTORY of the dispatching repo still finds
    that repo's Jobs (the 70-minute-inheritance case). None when nothing
    resolves.

    Args:
        event: The parsed hook event.

    Returns:
        The resolved workspace root, or None.
    """
    for candidate in (
        event.get("cwd"),
        os.environ.get("CLAUDE_PROJECT_DIR"),
        os.getcwd(),
    ):
        if not candidate or not isinstance(candidate, str):
            continue
        try:
            return state.resolve_workspace_root(candidate)
        except ChinamaxError:
            continue
    return None


def sweep_stale_supervision(event: dict) -> None:
    """Reap this session's Jobs whose Bridge has stopped supervising them.

    The stale-supervision sweep the SessionStart, Stop and UserPromptSubmit hooks
    run in-process (ADR 0003/0004, amended 2026-07-31): a Job must not outlive its
    Bridge, and the Bridge's long-poll heartbeat is what a dead Bridge stops
    refreshing. Keyed on the event's ``session_id`` (skipped when absent — a sweep
    with no live session to own has nothing to reap), and degraded to a stderr
    diagnostic on any failure so a sweep error never suppresses the hook's own
    output or fails the turn.

    Args:
        event: The parsed hook event.
    """
    session = event.get("session_id")
    if not session:
        return
    try:
        state.reap_stale_supervision(str(session))
    except Exception as error:  # noqa: BLE001 - a sweep must never fail the hook
        print(
            f"chinamax stale-supervision sweep: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
