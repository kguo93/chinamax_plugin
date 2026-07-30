"""Session-lifecycle and Bridge-enforcement hook entrypoints and shared helpers.

Every hook is a python entrypoint (unit-testable with crafted stdin JSON) run
through the plugin's shims: `session_start` (registry + orphan reap + digest),
`session_end` (session reap; ADR 0004, reversed 2026-07-30), `stop` (running-Job
notice), `user_prompt` (the live-Bridge roster into main), and `bridge_contract`
(the classification contract re-injected into the Bridge). They read Job state
through the ONE shared tolerant enumeration seam `state.list_jobs_tolerant`,
resolve "THIS workspace" the same three-rung way, and degrade to a clean exit
rather than ever putting a traceback in Claude's context.
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


def debug_breadcrumb(tag: str, **fields: object) -> None:
    """TEMPORARY diagnostic (revert after E5.7 diagnosis).

    Append one line to ``~/chinamax-hook-debug.log`` capturing hook lifecycle.
    Never raises — a breadcrumb must not perturb the behaviour being diagnosed.
    """
    try:
        import time

        parts = [f"{time.time():.3f}", tag, f"pid={os.getpid()}", f"ppid={os.getppid()}"]
        parts += [f"{key}={value}" for key, value in fields.items()]
        path = os.path.join(os.path.expanduser("~"), "chinamax-hook-debug.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(" ".join(str(part) for part in parts) + "\n")
    except Exception:  # noqa: BLE001 - diagnostics must never fail the hook
        pass


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
