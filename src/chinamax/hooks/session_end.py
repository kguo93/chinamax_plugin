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
from chinamax.hooks import read_event, resolve_event_host
from chinamax.host import Host


def main() -> int:
    """Reap the ending session's Jobs and remove its registry. Always exit 0.

    Returns:
        0 always. A SessionEnd hook must never block a session ending, so a
        whole-hook failure sends diagnostics to stderr and still exits 0.
    """
    try:
        event = read_event()
        context = resolve_event_host(event)
        if context is None:
            return 0
        session = event.get("session_id")
        if session:
            if context.host is Host.CODEX:
                _detach_codex_reaper(
                    str(session),
                    state.read_session_token(str(session)) or state.session_token(),
                )
                return 0
            else:
                state.reap_session(str(session))
            state.remove_session_registry(
                str(session),
                expected_token=(
                    state.read_session_token(str(session))
                    if context.host is Host.CODEX
                    else None
                ),
            )
    except Exception as error:  # noqa: BLE001 - never block session end
        print(
            f"chinamax session_end hook: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
    return 0


def _detach_codex_reaper(session: str, token: str | None) -> None:
    """Start the bounded Codex reaper without delaying SessionEnd."""
    import subprocess

    argv = [
        state.worker_python(),
        "-m",
        "chinamax",
        "reap",
        "--session",
        session,
        "--lock-path",
        str(state.sessions_dir() / "codex-reaper.lock"),
    ]
    if token:
        argv.extend(["--token", token])
    options = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(argv, **options)


if __name__ == "__main__":
    sys.exit(main())
