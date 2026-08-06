"""Pure helpers for the native Codex Host adapter.

The Host-native skill owns actual tool calls. These helpers keep naming,
permission, and spawn invariants deterministic and testable without requiring a
live Codex session.
"""

from __future__ import annotations

import re


class CodexPermissionError(RuntimeError):
    """Raised before a Codex mutating adapter action outside yolo."""


def require_bypass_permissions(permission_mode: str | None) -> None:
    """Require Codex's live ``bypassPermissions`` mode for mutation."""
    if permission_mode != "bypassPermissions":
        raise CodexPermissionError(
            "ChinamaX task/setup mutation requires codex --yolo; yolo disables "
            "Codex approval/sandbox enforcement. --read-only is enforced by the "
            "ChinamaX Runtime, not by Codex's sandbox."
        )


def slugify_task_name(value: str, *, max_length: int = 48) -> str:
    """Convert a task/profile label into Codex's lowercase underscore grammar."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    slug = slug[:max_length].rstrip("_")
    if not slug:
        raise ValueError("task name becomes empty after Codex slugification")
    return slug


def bridge_name(
    profile: str,
    task: str,
    *,
    existing: set[str] | frozenset[str] = frozenset(),
    max_length: int = 64,
) -> str:
    """Build a deterministic underscore-safe Bridge name with meaningful collisions."""
    profile_slug = slugify_task_name(profile, max_length=24)
    task_slug = slugify_task_name(task, max_length=max_length)
    prefix = f"chinamax_{profile_slug}_"
    base = f"{prefix}{task_slug}"[:max_length].rstrip("_")
    if base not in existing:
        return base
    words = [word for word in task_slug.split("_") if word]
    for word in words + ["follow_up", "task", "bridge"]:
        candidate = f"{prefix}{task_slug}_{word}"[:max_length].rstrip("_")
        if candidate not in existing:
            return candidate
    raise ValueError(f"Codex Bridge name collision cannot be disambiguated: {base}")


def spawn_spec(profile: str, task: str) -> dict[str, object]:
    """Return the fixed Codex Bridge spawn settings."""
    return {
        "task_name": bridge_name(profile, task),
        "model": "gpt-5.6-terra",
        "reasoning_effort": "low",
        "fork_turns": "none",
    }


def exact_bridge_address(message: str, live_names: set[str] | frozenset[str]) -> str | None:
    """Return exactly one complete live Bridge name mentioned by a message."""
    matches = [name for name in live_names if name and name in message]
    return matches[0] if len(matches) == 1 else None

