"""PreToolUse(Bash) hook: re-inject the Bridge classification contract (ADR 0010).

Fires on every Bash call in every session; the shim fast-paths past a non-Bridge
event without launching python. Here we parse the event and, ONLY when
``agent_type`` contains ``chinamax`` — a NAMED spawn makes Claude Code put the
teammate NAME (``chinamax-<profile>-<slug>``) in ``agent_type``, not the
``chinamax:chinamax`` subagent type — emit the contract as subagent-scoped
``additionalContext``. This is REINFORCEMENT, not a gate: additionalContext lands
for the Bridge's NEXT turn — it re-anchors a drifting haiku Bridge every poll
cycle but cannot veto the already-chosen call. The operative first copy is the
`commands/task.md` spawn prompt; no hard blocks (ADR 0010).
"""

from __future__ import annotations

import json
import sys

from chinamax.hooks import read_event

#: A NAMED Bridge's agent_type is its teammate NAME (chinamax-<profile>-<slug>),
#: never the "chinamax:chinamax" subagent type — Claude Code puts the name in the
#: payload. So the filter keys on the "chinamax" substring every Bridge name carries.
BRIDGE_AGENT_MARKER = "chinamax"

#: The single source of the injected contract text — the D4 test imports THIS
#: constant and it is one of the three lockstep members `test_task_command.py`
#: audits, so there is no fourth hand-maintained copy to drift.
CONTRACT = (
    "CHINAMAX BRIDGE CONTRACT — follow exactly.\n"
    "You relay between main and ONE worker Job lineage. Never do the task "
    "yourself, never spawn agents, never touch files.\n"
    "Classify each message from main as exactly one:\n"
    '1. CANCEL — the whole message says abandon the run ("cancel", "stop the '
    'job", "kill it", "never mind"). Run `$PY -m chinamax cancel <your-id>`, '
    "poll to terminal, fetch result, relay.\n"
    "2. OUT-OF-SCOPE — wants another model/profile, a different model string, or "
    "a new unrelated task. Make "
    "NO seam call. Send ONE SendMessage(to='main'): out of scope, dispatch a new "
    "/chinamax:task.\n"
    "3. STEER — Job still running, message is an instruction. Run "
    "`$PY -m chinamax steer <id>` with the message verbatim on stdin (quoted "
    "heredoc). Send NOTHING. Keep polling. If steer reports the Job already ended "
    "(not delivered), switch to RESUME with the SAME message; mention the "
    "possible duplicate when relaying the source Job's result.\n"
    "4. RESUME — Job already ended. Run `$PY -m chinamax resume <id>` with the "
    "message verbatim on stdin. Poll the NEW Job id it prints.\n"
    "Unsure between cancel and steer → STEER.\n"
    "Any OTHER refusal (e.g. lineage still running, not resumable): send the "
    "refusal text as your ONE SendMessage(to='main') and stop. Never retry a "
    "refused verb.\n"
    "After any verb: wait for terminal (`status <id> --wait --timeout-ms 120000`, "
    "Bash timeout 180000; exit 0 terminal, 2 poll again, 1 report once and stop). "
    "Then `result <id>`, strip the first header line, send EXACTLY ONE "
    "SendMessage(to='main') with the rest UNTOUCHED (or the failure/cancelled "
    "report). Never message main before terminal. No progress messages, no "
    "acknowledgments."
)


def main() -> int:
    """Emit the Bridge contract for a chinamax Bridge Bash call; silent otherwise.

    Returns:
        0 always. A non-Bridge event (the common case) emits nothing; a parse
        failure sends diagnostics to stderr and still exits 0.
    """
    try:
        event = read_event()
    except Exception as error:  # noqa: BLE001 - never block a tool call
        print(
            f"chinamax bridge_contract hook: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 0
    if BRIDGE_AGENT_MARKER not in (event.get("agent_type") or ""):
        return 0
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": CONTRACT,
                }
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
