"""Stop hook: a non-blocking notice of running Jobs (ADR 0010).

Emits a single JSON object carrying ONLY ``systemMessage`` (Claude Code's
non-blocking, operator-visible field) and never a ``decision`` key — so the
notice is operator-facing and can never block a turn. Lists ACTIVE Jobs only: an
interrupted Job is not in flight, so nagging about it every turn would be noise
(SessionStart still surfaces it).

It first runs the stale-supervision sweep (ADR 0003/0004, amended 2026-07-31), so
a Job whose Bridge died is marked `interrupted` before the active filter and thus
drops out of the notice.
"""

from __future__ import annotations

import json
import sys

from chinamax import state
from chinamax.hooks import (
    read_event,
    resolve_event_host,
    resolve_workspace,
    sweep_stale_supervision,
)


def main() -> int:
    """Emit the running-Jobs notice, degrading to a clean exit.

    Returns:
        0 always. A whole-hook failure leaves stdout empty and diagnostics on
        stderr; a Stop hook never blocks.
    """
    try:
        event = read_event()
        if resolve_event_host(event) is None:
            return 0
        message = _build_notice(event)
    except Exception as error:  # noqa: BLE001 - never block a turn
        print(f"chinamax stop hook: {type(error).__name__}: {error}", file=sys.stderr)
        return 0
    if message:
        sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
    return 0


def _build_notice(event: dict) -> str | None:
    """Return the notice for this workspace's active Jobs, or None when there are none.

    Bridge-first: each active Job is named by its owning Bridge (falling back to
    the Job id for a direct dispatch), and the pointer is to message the Bridge
    or run /chinamax:status — the /chinamax:cancel command no longer exists, a
    running Job is stopped by messaging its Bridge to abandon it.
    """
    root = resolve_workspace(event)
    if root is None:
        return None
    # Reap this session's stale-supervised Bridges before the active filter, so a
    # dead Bridge's Job drops out of the notice. When root is None the early
    # return above skips this — harmless: the session-keyed user_prompt sweep
    # covers that event. Guarded internally on the event's session_id.
    sweep_stale_supervision(event)
    records, _ = state.list_jobs_tolerant(root)
    active = sorted(
        (record for record in records if state.effective_status(record) in state.ACTIVE_STATUSES),
        key=lambda record: record["id"],
    )
    if not active:
        return None
    names = ", ".join(record.get("bridgeName") or record["id"] for record in active)
    subject = "chinamax Jobs are" if len(active) > 1 else "A chinamax Job is"
    return (
        f"{subject} still running in this workspace: {names}. "
        "Message the Bridge to steer or abandon it, or see /chinamax:status, "
        "before ending."
    )


if __name__ == "__main__":
    sys.exit(main())
