"""Codex Host adapter guard for spawn/setup tool calls."""

from __future__ import annotations

import json
import sys

from chinamax.codex import codex_permission_mode
from chinamax.hooks import read_event, resolve_event_host
from chinamax.host import Host


REFUSAL = (
    "ChinamaX Codex task/setup mutation requires permission_mode="
    "bypassPermissions; rerun Codex with codex --yolo. Yolo disables Codex "
    "approval/sandbox enforcement. ChinamaX depends on detached workers, "
    "polling, endpoint access, and persistent state; --read-only is enforced "
    "by the ChinamaX Runtime, not by the Codex sandbox."
)


def main() -> int:
    """Guard supported Codex mutation calls and remain a Claude no-op."""
    event = read_event()
    explicit = str(event.get("host") or event.get("host_name") or "").lower()
    if explicit == Host.CLAUDE.value:
        return 0
    context = resolve_event_host(event)
    if context is None or context.host is not Host.CODEX:
        return 0
    permission = codex_permission_mode(event)
    tool = str(
        event.get("tool_name")
        or event.get("toolName")
        or event.get("name")
        or event.get("function_name")
        or ""
    ).lower()
    command = str(event.get("command") or event.get("input") or "").lower()
    mutating = any(token in tool for token in ("agent", "spawn_agent", "setup"))
    mutating = mutating or ("chinamax-setup" in command and "apply" in command)
    if mutating and permission != "bypassPermissions":
        sys.stdout.write(json.dumps({"decision": "block", "reason": REFUSAL}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
