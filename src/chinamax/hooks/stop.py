"""Stop hook: a non-blocking notice of running Jobs (ADR 0010).

Emits a single JSON object carrying ONLY ``systemMessage`` (Claude Code's
non-blocking, operator-visible field) and never a ``decision`` key — so the
notice is operator-facing and can never block a turn. Lists ACTIVE Jobs only: an
interrupted Job is not in flight, so nagging about it every turn would be noise
(SessionStart still surfaces it).
"""

from __future__ import annotations

import json
import sys

from chinamax import state
from chinamax.hooks import read_event, resolve_workspace


def main() -> int:
    """Emit the running-Jobs notice, degrading to a clean exit.

    Returns:
        0 always. A whole-hook failure leaves stdout empty and diagnostics on
        stderr; a Stop hook never blocks.
    """
    try:
        message = _build_notice(read_event())
    except Exception as error:  # noqa: BLE001 - never block a turn
        print(f"chinamax stop hook: {type(error).__name__}: {error}", file=sys.stderr)
        return 0
    if message:
        sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
    return 0


def _build_notice(event: dict) -> str | None:
    """Return the notice for this workspace's active Jobs, or None when there are none."""
    root = resolve_workspace(event)
    if root is None:
        return None
    records, _ = state.list_jobs_tolerant(root)
    active = sorted(
        (record for record in records if state.effective_status(record) in state.ACTIVE_STATUSES),
        key=lambda record: record["id"],
    )
    if not active:
        return None
    ids = ", ".join(record["id"] for record in active)
    subject = "chinamax Jobs are" if len(active) > 1 else "A chinamax Job is"
    return (
        f"{subject} still running in this workspace: {ids}. "
        f"See /chinamax:status; use /chinamax:cancel <id> to stop one before ending."
    )


if __name__ == "__main__":
    sys.exit(main())
