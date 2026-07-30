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

import sys

from chinamax import state
from chinamax.hooks import read_event


def main() -> int:
    """Reap the ending session's Jobs and remove its registry. Always exit 0.

    Returns:
        0 always. A SessionEnd hook must never block a session ending, so a
        whole-hook failure sends diagnostics to stderr and still exits 0.
    """
    try:
        session = read_event().get("session_id")
        if session:
            state.reap_session(str(session))
            state.remove_session_registry(str(session))
    except Exception as error:  # noqa: BLE001 - never block session end
        print(
            f"chinamax session_end hook: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
