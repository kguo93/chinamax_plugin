"""Host selection and Host-scoped ChinamaX paths.

The Runtime is shared by Claude Code and Codex, but their configuration and
durable state must never be mixed.  This module resolves the Host once at the
process boundary and provides the immutable context consumed by the rest of
the Runtime.
"""

from __future__ import annotations

import os
import ntpath
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Mapping


class Host(StrEnum):
    """Supported Host adapters."""

    CLAUDE = "claude"
    CODEX = "codex"


class HostResolutionError(ValueError):
    """Raised when a Host marker is missing or malformed."""


def _absolute_dir(environ: Mapping[str, str], name: str) -> Path | None:
    value = environ.get(name, "").strip()
    if not value or not _is_absolute(value):
        return None
    return Path(value).expanduser()


def _is_absolute(value: str) -> bool:
    """Check an env path using the selected platform's path grammar."""
    return ntpath.isabs(value) if sys.platform == "win32" else os.path.isabs(value)


def _join_native(base: Path, *parts: str) -> Path:
    """Compose native Windows drive/UNC paths even when tests mock ``win32``."""
    if sys.platform == "win32":
        text = str(base)
        drive, _ = ntpath.splitdrive(text)
        if drive or text.startswith("\\\\"):
            return Path(str(PureWindowsPath(text, *parts)))
    return base.joinpath(*parts)


def _native_home(environ: Mapping[str, str]) -> Path:
    """Return the native user home, ignoring Git Bash's synthetic ``HOME`` on Windows."""
    if sys.platform == "win32":
        userprofile = environ.get("USERPROFILE", "").strip()
        if userprofile and _is_absolute(userprofile):
            return Path(userprofile).expanduser()
        return Path.home()
    home_value = environ.get("HOME", "").strip()
    return Path(home_value).expanduser() if os.path.isabs(home_value) else Path.home()


def _fallback_data_root(host: Host, home: Path, environ: Mapping[str, str]) -> Path:
    """Return the native platform fallback for one Host's durable data."""
    suffix = "chinamax-codex" if host is Host.CODEX else "chinamax"
    if sys.platform == "win32":
        local = environ.get("LOCALAPPDATA", "").strip()
        base = (
            Path(local).expanduser()
            if local and _is_absolute(local)
            else _join_native(home, "AppData", "Local")
        )
        return _join_native(base, suffix)
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / suffix
    xdg = _absolute_dir(environ, "XDG_STATE_HOME")
    return xdg / suffix if xdg is not None else home / ".local" / "state" / suffix


def _event_value(event: Mapping[str, object] | None, *names: str) -> str:
    if not event:
        return ""
    for name in names:
        value = event.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _has_codex_evidence(environ: Mapping[str, str], event: Mapping[str, object] | None) -> bool:
    if _absolute_dir(environ, "PLUGIN_ROOT") or _absolute_dir(environ, "PLUGIN_DATA"):
        return True
    host = _event_value(event, "host", "host_name", "hostName")
    if host.lower() == Host.CODEX.value:
        return True
    return bool(
        _event_value(
            event,
            "plugin_root",
            "pluginRoot",
            "plugin_data",
            "pluginData",
            "codex_plugin_root",
            "codexPluginRoot",
        )
    )


def _has_claude_evidence(environ: Mapping[str, str], event: Mapping[str, object] | None) -> bool:
    if _absolute_dir(environ, "CLAUDE_PLUGIN_ROOT") or _absolute_dir(
        environ, "CLAUDE_PLUGIN_DATA"
    ):
        return True
    host = _event_value(event, "host", "host_name", "hostName")
    return host.lower() == Host.CLAUDE.value


@dataclass(frozen=True)
class HostContext:
    """Immutable Host selection and all Host-specific filesystem roots."""

    host: Host
    plugin_root: Path | None
    data_root: Path
    state_root: Path
    keys_path: Path
    overlay_path: Path
    interpreter_path: Path

    @classmethod
    def from_host(
        cls, host: Host, environ: Mapping[str, str] | None = None
    ) -> "HostContext":
        environ = os.environ if environ is None else environ
        home = _native_home(environ)
        if host is Host.CODEX:
            plugin_root = _absolute_dir(environ, "PLUGIN_ROOT")
            plugin_data = _absolute_dir(environ, "PLUGIN_DATA")
            fallback = _fallback_data_root(host, home, environ)
            data_root = plugin_data or fallback
            state_root = _join_native(plugin_data, "state") if plugin_data is not None else fallback
            home_dir = _join_native(home, ".codex")
        else:
            plugin_root = _absolute_dir(environ, "CLAUDE_PLUGIN_ROOT")
            plugin_data = _absolute_dir(environ, "CLAUDE_PLUGIN_DATA")
            fallback = _fallback_data_root(host, home, environ)
            data_root = plugin_data or fallback
            state_root = _join_native(plugin_data, "state") if plugin_data is not None else fallback
            home_dir = _join_native(home, ".claude")
        return cls(
            host=host,
            plugin_root=plugin_root,
            data_root=data_root,
            state_root=state_root,
            keys_path=_join_native(home_dir, "model-keys.env"),
            overlay_path=_join_native(home_dir, "chinamax-profiles.json"),
            interpreter_path=_join_native(data_root, "python-path"),
        )


_CURRENT: ContextVar[HostContext | None] = ContextVar("chinamax_host", default=None)
_CURRENT_SIGNATURE: ContextVar[tuple[str, ...] | None] = ContextVar(
    "chinamax_host_environment", default=None
)


def _environment_signature(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    environ = os.environ if environ is None else environ
    return tuple(environ.get(name, "") for name in (
        "HOME",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_PLUGIN_DATA",
        "PLUGIN_ROOT",
        "PLUGIN_DATA",
        "XDG_STATE_HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
    ))


def set_current_host(context: HostContext) -> None:
    """Bind the process's resolved Host context for shared helper modules."""
    _CURRENT.set(context)
    _CURRENT_SIGNATURE.set(_environment_signature())


def current_host(*, compatibility: bool = True) -> HostContext:
    """Return the process Host, optionally preserving direct Python compatibility.

    CLI and hook entrypoints always call :func:`resolve_host` first.  The
    compatibility fallback is only for existing library-level callers and test
    fixtures that call Runtime helpers directly without going through a Host
    adapter; it does not affect direct CLI resolution.
    """
    context = _CURRENT.get()
    if context is not None:
        # A long-lived Python test process may change HOME/plugin-data between
        # direct helper calls. Re-resolve paths for the already selected Host,
        # never the Host itself; a constructed Claude context cannot be
        # switched to Codex by ambient variables.
        if _CURRENT_SIGNATURE.get() != _environment_signature():
            refreshed = HostContext.from_host(context.host)
            _CURRENT.set(refreshed)
            _CURRENT_SIGNATURE.set(_environment_signature())
            return refreshed
        return context
    if compatibility:
        return HostContext.from_host(Host.CLAUDE)
    raise HostResolutionError("no Host has been resolved; require --host claude|codex")


def resolve_host(
    explicit: str | Host | None = None,
    environ: Mapping[str, str] | None = None,
    hook_event: Mapping[str, object] | None = None,
) -> HostContext:
    """Resolve a Host using explicit marker, env marker, then plugin evidence.

    Codex-native ``PLUGIN_*`` evidence wins when both plugin variable families
    are present because Codex exposes Claude-compatible aliases as well.
    """
    environ = os.environ if environ is None else environ
    if explicit is not None and str(explicit).strip():
        value = str(explicit).strip().lower()
        try:
            host = Host(value)
        except ValueError as exc:
            raise HostResolutionError(
                f"unknown Host {explicit!r}; require --host claude|codex"
            ) from exc
        return HostContext.from_host(host, environ)

    marker = environ.get("CHINAMAX_HOST", "").strip().lower()
    if marker:
        try:
            host = Host(marker)
        except ValueError as exc:
            raise HostResolutionError(
                f"invalid CHINAMAX_HOST {marker!r}; require claude or codex"
            ) from exc
        return HostContext.from_host(host, environ)

    if _has_codex_evidence(environ, hook_event):
        return HostContext.from_host(Host.CODEX, environ)
    if _has_claude_evidence(environ, hook_event):
        return HostContext.from_host(Host.CLAUDE, environ)
    raise HostResolutionError(
        "cannot determine ChinamaX Host; require --host claude|codex "
        "or set CHINAMAX_HOST"
    )
