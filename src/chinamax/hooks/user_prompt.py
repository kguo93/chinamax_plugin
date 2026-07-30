"""UserPromptSubmit hook: inject the live-Bridge roster + routing rule into main.

Fires only in the main session. Scans every workspace store for THIS session's
Jobs carrying a ``bridgeName``, groups by Bridge, and keeps each Bridge's NEWEST
Job — one roster row per Bridge, never one per Job, and INCLUDING terminal ones
(a persistent Bridge whose Job finished is still alive and messageable; a message
to it becomes a resume). It injects the roster plus the explicit-addressing
routing rule as ``additionalContext`` (inserted before the model processes the
prompt). NO output at all when the session owns no bridge-named Jobs.
"""

from __future__ import annotations

import json
import sys

from chinamax import state
from chinamax.hooks import read_event

#: The routing rule injected alongside the roster: main forwards to a Bridge only
#: when the operator addresses it, and never does a worker's task itself.
ROUTING_RULE = (
    "Forward a message to a Bridge via SendMessage ONLY when the operator "
    'addresses it — by teammate name, its profile, or "the bridge/worker". '
    "Never act on a worker's task yourself; display each Bridge relay verbatim. "
    "Anything not addressed to a Bridge is your own work."
)


def main() -> int:
    """Emit the live-Bridge roster additionalContext, degrading to a clean exit.

    Returns:
        0 always. A whole-hook failure leaves stdout empty and diagnostics on
        stderr, never a traceback into the prompt turn.
    """
    try:
        context = _build_context(read_event())
    except Exception as error:  # noqa: BLE001 - never fail the prompt turn
        print(
            f"chinamax user_prompt hook: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 0
    if context:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": context,
                    }
                }
            )
            + "\n"
        )
    return 0


def _build_context(event: dict) -> str | None:
    """Return the roster additionalContext, or None when there are no Bridges."""
    session = event.get("session_id")
    if not session:
        return None
    bridges = _session_bridges(str(session))
    if not bridges:
        return None
    rows = ", ".join(_roster_row(record) for record in bridges)
    return f"Live chinamax Bridges: {rows}. {ROUTING_RULE}"


def _session_bridges(session_id_value: str) -> list[dict]:
    """Return the newest Job per Bridge this session owns, sorted by Bridge name."""
    newest: dict[str, dict] = {}
    for store in state.iter_workspace_stores():
        records, _ = store.load_records()
        for record in records:
            if record.get("sessionId") != session_id_value:
                continue
            bridge = record.get("bridgeName")
            if not bridge:
                continue
            current = newest.get(bridge)
            if current is None or state.latest_key(record) > state.latest_key(current):
                newest[bridge] = record
    return [newest[name] for name in sorted(newest)]


def _roster_row(record: dict) -> str:
    """Render one Bridge's roster entry — active, or idle and messageable."""
    bridge = record.get("bridgeName")
    status = state.effective_status(record)
    if status in state.ACTIVE_STATUSES:
        return f"{bridge} ({status})"
    return f"{bridge} (idle — {status}; message it to follow up)"


if __name__ == "__main__":
    sys.exit(main())
