"""SessionEnd hook: kill the ending session's active Jobs, drop its registry.

Jobs are session-scoped (ADR 0004, reversed 2026-07-30): a session ending —
including ``/clear``, which fires SessionEnd with reason ``clear`` — kills its
still-active Jobs (whole process tree) and marks their records ``cancelled``.
There is no reason filtering. Ordering is the safety net and is MANDATORY: reap
FIRST, registry removal LAST, so a hook killed mid-reap leaves the registry in
place and every straggler (including a Job dispatched during the reap) degrades
to the SessionStart orphan path at the next start. The reap runs with the short
`state.SESSION_REAP_GRACE_S`/`SESSION_REAP_CONFIRM_S` so it fits the hook budget.
"""

from __future__ import annotations

import os
import sys

from chinamax import state
from chinamax.hooks import debug_breadcrumb, read_event


def main() -> int:
    """Reap the ending session's Jobs and remove its registry. Always exit 0.

    Returns:
        0 always. A SessionEnd hook must never block a session ending, so a
        whole-hook failure sends diagnostics to stderr and still exits 0.
    """
    try:
        event = read_event()
        session = event.get("session_id")
        debug_breadcrumb(
            "SESSION_END-PY-ENTRY",
            session=session,
            reason=event.get("reason"),
            cpd=os.environ.get("CLAUDE_PLUGIN_DATA", "<unset>"),
            data_root=str(state.state_root()),
            cwd=os.getcwd(),
        )
        if session:
            result = state.reap_session(str(session))
            debug_breadcrumb("SESSION_END-PY-REAPED", session=session, result=repr(result))
            state.remove_session_registry(str(session))
            debug_breadcrumb("SESSION_END-PY-REGISTRY-REMOVED", session=session)
        debug_breadcrumb("SESSION_END-PY-DONE", session=session)
    except Exception as error:  # noqa: BLE001 - never block session end
        debug_breadcrumb("SESSION_END-PY-ERROR", err=f"{type(error).__name__}: {error}")
        print(
            f"chinamax session_end hook: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
