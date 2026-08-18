"""PreToolUse(Bash) hook: re-inject the Bridge classification contract (ADR 0010).

Fires on every Bash call in every session; the shim fast-paths past a non-Bridge
event without launching python. Here we parse the event and, ONLY when
``agent_type`` contains ``chinamax`` — Claude Code sets ``agent_type`` to the
subagent TYPE, which for this plugin's Bridge is ``chinamax:chinamax`` (added to
PreToolUse in CLI 2.1.218); a ``chinamax-<profile>-<slug>`` teammate name also
carries the marker — emit the contract as subagent-scoped ``additionalContext``. This is REINFORCEMENT, not a gate: additionalContext lands
for the Bridge's NEXT turn — it re-anchors a drifting Bridge every poll
cycle but cannot veto the already-chosen call. The operative first copy is the
`commands/task.md` spawn prompt; no hard blocks (ADR 0010).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from chinamax.hooks import read_event, resolve_event_host

#: The Bridge's agent_type is the subagent TYPE "chinamax:chinamax" (a
#: chinamax-<profile>-<slug> teammate name also carries the marker); either way it
#: contains "chinamax", so the filter keys on that substring. NOTE: hooks.json's
#: SubagentStart matcher must ALSO catch the colon-form type (ADR 0010, 2026-08-17).
BRIDGE_AGENT_MARKER = "chinamax"

def canonical_contract_path() -> Path | None:
    """Resolve the maintained Host adapter contract at runtime."""
    root = os.environ.get("PLUGIN_ROOT", "").strip() or os.environ.get(
        "CLAUDE_PLUGIN_ROOT", ""
    ).strip()
    if not root:
        # Test/development fallback; installed Hosts provide their plugin root.
        root = str(Path(__file__).resolve().parents[3])
    path = Path(root) / "skills" / "chinamax-bridge" / "SKILL.md"
    return path if path.is_file() else None


def load_contract() -> str:
    """Read the canonical contract, or return empty for a safe no-op."""
    path = canonical_contract_path()
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


CONTRACT = load_contract()


def main() -> int:
    """Emit the Bridge contract for a chinamax Bridge Bash call; silent otherwise.

    Returns:
        0 always. A non-Bridge event (the common case) emits nothing; a parse
        failure sends diagnostics to stderr and still exits 0.
    """
    try:
        event = read_event()
        if resolve_event_host(event) is None:
            return 0
    except Exception as error:  # noqa: BLE001 - never block a tool call
        print(
            f"chinamax bridge_contract hook: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 0
    if not CONTRACT or BRIDGE_AGENT_MARKER not in (event.get("agent_type") or ""):
        return 0
    event_name = str(event.get("hook_event_name") or "PreToolUse")
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": CONTRACT,
                }
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
