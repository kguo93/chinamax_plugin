"""Durable Job state: the per-workspace store, its lock, and the derived index.

Per-Job records under ``jobs/`` are the source of truth; ``state.json`` is a
derived id cache any reader may rebuild. Every mutation goes through one locked
compare-and-swap updater — re-read, check the expected state, publish by
tmp+rename — so a late heartbeat can never resurrect ``running`` over a terminal
write. The lock is a dedicated ``state.lock`` sidecar and never ``state.json``
itself: renaming over a locked file leaves the holder locking an unlinked inode
and silently voids mutual exclusion.

Selection and liveness live here too, in ONE place: the shared id resolver, the
"latest" ordering key, the provably-gone predicate and the `effective_status`
that derives `interrupted` from it. Every verb reads them rather than deriving
its own, so `result`/`cancel`/`resume` cannot drift from `status`/`logs`.

Jobs are session-scoped (ADR 0004, reversed 2026-07-30): records carry their
owning ``sessionId`` and `reap_session`/`reap_orphans` end a dead session's
still-active Jobs, keyed off a session-liveness registry under
``<data root>/sessions/`` that records each live Claude process. Pruning removes
only FINISHED Jobs beyond the cap — now including reaped, STORED-`interrupted`
records; a DERIVED-`interrupted` crash keeps its resumable Thread.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from chinamax import ChinamaxError
from chinamax.transcript import merge_follow_up, read_repaired_messages, write_messages

#: Bumped only for a breaking record change; the schema is additive otherwise —
#: readers ignore unknown fields and default missing optional ones, so an old
#: record stays readable across plugin upgrades (PRD user story 13).
SCHEMA_VERSION = 1

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: Either DERIVED read-side (a non-terminal Job whose worker is provably gone and
#: whose heartbeat has aged past the grace — the crash path, whose stored status
#: stays `running`/`queued` so the Thread stays resumable) OR STORED by
#: `reap_orphans` when a dead session's Job is reaped (ADR 0004, reversed
#: 2026-07-30 — a policy-dead Thread, terminal and prunable). `effective_status`
#: derives the read-side case; the stored case is a terminal status like the rest.
STATUS_INTERRUPTED = "interrupted"

#: A Job in one of these will never move again on its own. STORED `interrupted`
#: joins them (ADR 0004, reversed): a reaped Thread is policy-dead. A DERIVED-
#: interrupted record still stores `running`/`queued`, so only prune grading —
#: which keys on the STORED status — treats the reaped case as finished.
TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED}
)
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})
#: The stored statuses pruning grades as finished — including STORED `interrupted`
#: (a reaped Job, prunable). `_prune_locked` grades the STORED status, so a
#: DERIVED-interrupted record (stored `running`, crashed worker) is NOT graded
#: finished and keeps exactly the crash recovery it exists for.
FINISHED_STATUSES = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED}
)

#: Record writes are throttled to one per this interval, matching the
#: `status --wait` poll so a caller cannot outrun the record it polls.
RECORD_THROTTLE_S = 2.0
#: `updatedAt` is refreshed at least this often even when nothing changes. This
#: number and the throttle above move together with jobs/02's stale grace (twice
#: this interval) — drifting one apart breaks stale detection.
HEARTBEAT_INTERVAL_S = 30.0
#: How stale `updatedAt` must be before a provably-gone worker's Job reads as
#: `interrupted`. TWICE `HEARTBEAT_INTERVAL_S` — these two numbers move together
#: or stale detection breaks. The grace covers a single missed or delayed beat
#: around a crash, never a pid gap: the dispatcher records the pid immediately
#: after `Popen` and the worker's claim is an atomic locked CAS.
STALE_GRACE_S = 60.0
#: Newest finished Jobs kept per workspace. Active Jobs and DERIVED-`interrupted`
#: Jobs (stored `running`/`queued`) are never pruned and never counted; a reaped
#: STORED-`interrupted` Job is finished and prunable like any terminal record.
PRUNE_KEEP = 50
#: How long `cancel` waits after SIGTERM before escalating to SIGKILL.
TERMINATE_GRACE_S = 10.0
#: How long the SIGKILL sweep waits for its targets to actually disappear.
_TERMINATE_CONFIRM_S = 5.0
#: The SIGTERM grace and SIGKILL-confirm a session reap runs with — far shorter
#: than a cancel's, because a multi-Job reap under a SessionEnd/SessionStart hook
#: timeout cannot afford the 10 s + 5 s cancel budget (a hook killed mid-reap is
#: safe: the next SessionStart's re-reap is idempotent).
SESSION_REAP_GRACE_S = 2.0
SESSION_REAP_CONFIRM_S = 2.0
#: What a SessionEnd (or same-PID predecessor) reap stamps as a Job's reason.
SESSION_REAP_REASON = "Reaped: the owning Claude session ended."
#: What an orphan reap stamps when a dead session's Job is marked `interrupted`.
ORPHAN_REAP_REASON = "Interrupted: the owning Claude session is no longer alive."
#: Poll interval while waiting on a signalled process to go away.
_TERMINATE_POLL_S = 0.05

#: `status --wait` poll interval, on a monotonic clock.
POLL_INTERVAL_S = 2.0
#: The default `status --wait` bound (~4 min) applied when no `--timeout-ms` is
#: given — the seam default a direct `/chinamax:status --wait` call inherits.
WAIT_TIMEOUT_DEFAULT_MS = 240_000
#: The ceiling a larger `--timeout-ms` clamps to, lifted to the Bridge's 900 s
#: long-poll (ADR 0003) so that default poll is honored rather than capped, while
#: an unbounded value still cannot turn a bounded poll into an endless block.
WAIT_TIMEOUT_MS = 900_000

#: Records hold prompt text and results, so the store is owner-only throughout.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: The environment variable the SessionStart hook exports (`session_start.py`).
#: This IS the owning-session field: `reap_session`/`reap_orphans` key a Job's
#: lifecycle off the `sessionId` it populates (ADR 0004, reversed 2026-07-30).
SESSION_ID_VARIABLE = "CHINAMAX_SESSION_ID"
#: Overrides the interpreter the detached worker runs under. Tests point it at a
#: non-executable path to make a spawn fail for real, with no mocked process layer.
WORKER_PYTHON_VARIABLE = "CHINAMAX_WORKER_PYTHON"
#: Pins the epoch-ms a `steer` write stamps into its queue-file name. Test-only:
#: it lets two writers be forced into one millisecond so the random suffix — not
#: the millisecond — is what keeps their names distinct.
STEER_CLOCK_VARIABLE = "CHINAMAX_STEER_NOW_MS"
#: The subdirectory a drained steer is moved into, beside the still-queued files.
STEER_CONSUMED_DIRNAME = "consumed"

_ID_ALPHABET = string.digits + string.ascii_lowercase
#: The record scan matches this and NOT the bare glob ``jobs/*.json``, which
#: would swallow ``<id>.result.json`` and register phantom Jobs.
_RECORD_RE = re.compile(r"^(task-[0-9a-z]+-[0-9a-z]{6})\.json$")

#: A per-process counter ordering a same-process burst of steers landing inside
#: one millisecond. Zero-padded ``%04d`` in the filename so it sorts lexically —
#: unpadded, ``-10`` would sort before ``-2``.
_steer_seq_lock = threading.Lock()
_steer_seq_counter = 0
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: How far back `tail_lines` seeks: a 70-minute log is never read whole.
_TAIL_WINDOW_BYTES = 65536

#: Defaults a reader fills in for optional fields a record does not carry.
RECORD_DEFAULTS: dict[str, object] = {
    "schemaVersion": SCHEMA_VERSION,
    "id": None,
    "title": "",
    "profile": None,
    "write": True,
    "workspaceRoot": None,
    "sessionId": None,
    "bridgeName": None,
    "resumedFrom": None,
    "lineageRoot": None,
    "status": STATUS_QUEUED,
    "phase": None,
    "pid": None,
    "pidStartTime": None,
    "createdAt": None,
    "startedAt": None,
    "updatedAt": None,
    "completedAt": None,
    "logFile": None,
    "result": None,
    "errorMessage": None,
    "request": {},
}


def utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: object) -> float | None:
    """Parse one ISO-8601 record timestamp into epoch seconds.

    Args:
        value: The stored timestamp, or None when the field was never set.

    Returns:
        Epoch seconds, or None when there is nothing parsable.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def escape_control(text: str) -> str:
    """Escape newlines, CRs, ANSI escapes and every other control character.

    One progress event is always exactly one log line, so a tool's raw output
    can neither forge log entries nor rewrite a terminal rendering the preview.

    Args:
        text: Arbitrary text, possibly straight from a tool's stdout.

    Returns:
        The same text with every control character rendered as ``\\xNN``.
    """
    return _CONTROL_RE.sub(lambda match: f"\\x{ord(match.group()):02x}", str(text))


def summarize(prompt: str, limit: int = 80) -> str:
    """Render a Job's title: the prompt's first line, escaped and clipped."""
    first = escape_control(prompt.strip().splitlines()[0] if prompt.strip() else "")
    return first if len(first) <= limit else first[: limit - 1] + "…"


def render_failure(payload: dict) -> str:
    """Render runtime/03's structured failure payload as one compact line.

    The full JSON payload is already in ``jobs/<id>.log`` through the reporter;
    this compact ``classification``/``attempt_count``/``status_code``/
    ``exception_text`` rendering is what the record's ``errorMessage`` carries.

    Args:
        payload: The `liveness.RunFailure` payload.

    Returns:
        One control-character-free line.
    """
    parts = [
        f"{payload.get('classification') or 'unknown'}/"
        f"{payload.get('failure_kind') or 'unknown'}",
        f"after {payload.get('attempt_count')} attempt(s)",
    ]
    if payload.get("status_code") is not None:
        parts.append(f"HTTP {payload['status_code']}")
    rendered = ", ".join(parts)
    exception_text = payload.get("exception_text")
    return f"{rendered}: {escape_control(exception_text)}" if exception_text else rendered


def parse_stat_fields(stat_text: str) -> list[str] | None:
    """Return a ``/proc/<pid>/stat`` line's fields from field 3 onward.

    The split is taken AFTER THE LAST ``)``: field 2 is the parenthesized comm,
    which may itself contain spaces and parentheses. A naive whitespace split
    ships green through every test but the one that pins this, then feeds a
    wrong start-time into a kill decision. Every reader of this file — the
    start-time guard, the zombie test, and the process-tree walk — goes through
    this one split so none of them can index a different field set.

    Args:
        stat_text: The raw contents of ``/proc/<pid>/stat``.

    Returns:
        The fields after the comm (so field 3 is at index 0), or None.
    """
    close = stat_text.rfind(")")
    if close < 0:
        return None
    fields = stat_text[close + 1 :].split()
    return fields or None


def parse_pid_start_time(stat_text: str) -> int | None:
    """Return field 22 (``starttime``) of a ``/proc/<pid>/stat`` line.

    Args:
        stat_text: The raw contents of ``/proc/<pid>/stat``.

    Returns:
        The start time in clock ticks, or None when the line is unusable.
    """
    fields = parse_stat_fields(stat_text)
    return None if fields is None else _stat_start_time(fields)


def read_proc_stat(pid: int) -> list[str] | None:
    """Read one process's stat fields, or None when it cannot be read."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_stat_fields(text)


def read_pid_start_time(pid: int) -> int | None:
    """Read a process's start time, or None where ``/proc`` is unreadable.

    A non-Linux dev machine, or a child that exited before the dispatcher got to
    it, yields None rather than failing the dispatch — which leaves the
    ``kill(pid, 0)`` half of `worker_gone`'s liveness test.

    Args:
        pid: The process to inspect.

    Returns:
        The start time in clock ticks, or None.
    """
    fields = read_proc_stat(pid)
    return None if fields is None else _stat_start_time(fields)


@dataclass(frozen=True)
class ProcessInfo:
    """One process as ``/proc/<pid>/stat`` describes it, as far as it parses."""

    pid: int
    ppid: int
    pgid: int
    start_time: int | None
    zombie: bool


def read_process(pid: int) -> ProcessInfo | None:
    """Return one process's identity and links, or None when it is gone."""
    fields = read_proc_stat(pid)
    if fields is None or len(fields) < 3:
        return None
    try:
        ppid, pgid = int(fields[1]), int(fields[2])
    except ValueError:
        return None
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        pgid=pgid,
        start_time=_stat_start_time(fields),
        zombie=fields[0] == "Z",
    )


def process_table() -> dict[int, ProcessInfo]:
    """Snapshot every readable process, keyed by pid."""
    table: dict[int, ProcessInfo] = {}
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        info = read_process(int(entry.name))
        if info is not None:
            table[info.pid] = info
    return table


def descendants(
    root_pid: int, table: dict[int, ProcessInfo] | None = None
) -> dict[int, ProcessInfo]:
    """Return ``root_pid`` plus every process reachable from it by ppid links.

    This snapshot must be taken BEFORE anything is signalled: orphans reparent
    to init the instant their parent dies, so a walk afterwards finds nothing
    left to stop. Its reach is also its limit — a descendant that fully
    daemonized before the snapshot has already been reparented away and cannot
    be found here. The confined bash tool does not do that.

    Args:
        root_pid: The process to walk from.
        table: A pre-taken process table, or None to take one.

    Returns:
        The reachable processes, keyed by pid; empty when the root is gone.
    """
    table = process_table() if table is None else table
    children: dict[int, list[int]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
    found: dict[int, ProcessInfo] = {}
    queue = [root_pid]
    while queue:
        pid = queue.pop()
        if pid in found or pid not in table:
            continue
        found[pid] = table[pid]
        queue.extend(children.get(pid, ()))
    return found


def usable_pid(record: dict) -> int | None:
    """Return a record's pid when it may safely be probed and signalled.

    A record with no usable pid is NEVER probed and never signalled: pid 0
    addresses the caller's own process group, a negative pid addresses a group
    outright, and 1 is init — none of them can ever be a worker.

    Args:
        record: The Job record.

    Returns:
        The pid, or None.
    """
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    return pid


def worker_gone(record: dict) -> bool:
    """Report whether a record's worker is PROVABLY gone.

    The liveness half of `effective_status`, exposed on its own because a
    relaunch reclaim needs it WITHOUT the stale grace — an explicit relaunch
    verifies liveness directly rather than guarding against a delayed heartbeat.

    "Provably" is deliberately narrow, and the degradations are pinned rather
    than left to judgement:

    * ``ProcessLookupError`` (ESRCH) is the only signal that means gone. A
      ``PermissionError`` (EPERM) means the process EXISTS and is treated as
      alive.
    * A zombie still answers signal 0 and would otherwise read as alive forever,
      so ``/proc`` state ``Z`` counts as gone.
    * A ``pidStartTime`` that no longer matches is the pid-reuse case: the pid
      is alive, but it is somebody else's.
    * A record missing ``pidStartTime`` skips the reuse comparison and falls
      back to the ESRCH test alone.
    * A ``/proc`` read that fails after signal 0 succeeded means the process
      died mid-check, which counts as gone rather than as an error. That rule
      assumes the Linux deployment target the store already assumes: the
      dispatcher writes ``pidStartTime`` from the same ``/proc``.

    Args:
        record: The Job record.

    Returns:
        Whether the recorded worker is provably no longer running.
    """
    pid = usable_pid(record)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        # EPERM and anything else: the process answered, so it exists.
        return False
    info = read_process(pid)
    if info is None:
        return True
    if info.zombie:
        return True
    recorded = record.get("pidStartTime")
    if recorded is None or info.start_time is None:
        return False
    return info.start_time != recorded


def effective_status(record: dict, now: float | None = None) -> str:
    """Grade one record's status, deriving `interrupted` for a dead worker.

    Both non-terminal statuses are graded, not just ``running``: the dispatcher
    writes the child pid immediately after `Popen`, so a worker that died before
    claiming leaves a ``queued`` record whose recorded pid is dead — grading only
    ``running`` would strand that Job active forever.

    The test is conjunctive — provably gone AND a heartbeat older than the grace
    — so a live-but-quiet worker is never misreported.

    Args:
        record: The Job record.
        now: Epoch seconds to grade against; the current time when omitted.

    Returns:
        The stored status, or `STATUS_INTERRUPTED`.
    """
    status = record.get("status")
    if status not in ACTIVE_STATUSES or not worker_gone(record):
        return status
    updated = parse_timestamp(record.get("updatedAt"))
    reference = time.time() if now is None else now
    if updated is not None and reference - updated <= STALE_GRACE_S:
        return status
    return STATUS_INTERRUPTED


def is_active(record: dict, now: float | None = None) -> bool:
    """Report whether a Job may still make progress.

    An effectively-`interrupted` Job is NOT active to any verb, and no verb
    re-derives liveness for itself.
    """
    return effective_status(record, now) in ACTIVE_STATUSES


def latest_key(record: dict) -> tuple[float, float, str]:
    """Sort key for "latest", shared by every verb's default selection.

    ``completedAt`` first, falling back to ``updatedAt`` for records that have
    none — which a DERIVED-`interrupted` record never does (its stored status is
    still ``running``/``queued``), while a reaped STORED-`interrupted` one now
    carries ``completedAt`` like any terminal record — then ``createdAt``, then
    the id. Callers sort with ``reverse=True``.
    """
    primary = (
        parse_timestamp(record.get("completedAt"))
        or parse_timestamp(record.get("updatedAt"))
        or 0.0
    )
    return (primary, parse_timestamp(record.get("createdAt")) or 0.0, str(record.get("id") or ""))


def elapsed(record: dict) -> str:
    """Render a Job's elapsed time as ``<h>h<mm>m`` / ``<m>m<ss>s`` / ``<s>s``.

    Measured from ``startedAt`` once running and from ``createdAt`` while queued,
    and frozen at ``completedAt`` once terminal. Shared by ``status``'s per-Job
    line and the SessionStart digest so the two never render duration differently.

    Args:
        record: The Job record.

    Returns:
        The elapsed rendering, or ``-`` when no start time is known.
    """
    start = parse_timestamp(record.get("startedAt")) or parse_timestamp(
        record.get("createdAt")
    )
    if start is None:
        return "-"
    end = parse_timestamp(record.get("completedAt"))
    if end is None:
        end = time.time()
    seconds = int(max(0.0, end - start))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def render_job_row(record: dict) -> str:
    """Render one Job's bridge-first summary row.

    THE shared row helper: `status`, the `reap` digests and both session-hook
    digests render through it, so the three cannot diverge. The Bridge name leads
    (``-`` for a record without one — a direct dispatch); the id, effective
    status, phase, elapsed, profile and title follow in their prior order.

    Args:
        record: The Job record.

    Returns:
        The two-space-joined row.
    """
    return "  ".join(
        [
            str(record.get("bridgeName") or "-"),
            str(record["id"]),
            f"{effective_status(record):<11}",
            f"{(record.get('phase') or '-'):<14}",
            f"{elapsed(record):>8}",
            str(record.get("profile") or "-"),
            str(record.get("title") or ""),
        ]
    )


def terminate_tree(
    pid: int,
    start_time: int | None = None,
    grace_s: float = TERMINATE_GRACE_S,
    confirm_s: float = _TERMINATE_CONFIRM_S,
) -> list[int]:
    """Stop a worker's whole process TREE, and report what survived.

    The group is NOT the tree: the Runtime's bash tool starts every command in
    its OWN session, so a single ``killpg`` on the worker's group leaves those
    children running. The sweep therefore snapshots the descendants FIRST (an
    orphan reparents the instant its parent dies), SIGTERMs every distinct
    process group in that set, waits out the grace, RE-SCANS for descendants
    that appeared after the snapshot, and only then SIGKILLs what is left.

    A pid whose start time no longer matches where it was seen is skipped rather
    than signalled, so a recycled pid or pgid is never the target.

    Args:
        pid: The recorded worker pid.
        start_time: Its recorded ``pidStartTime``, when the record carries one.
        grace_s: How long to wait between SIGTERM and SIGKILL.
        confirm_s: How long to wait for the SIGKILL sweep's targets to disappear
            (a session reap passes a shorter value than a `cancel`'s default, so
            a multi-Job reap fits a hook's timeout budget).

    Returns:
        The pids still alive after the sweep — empty when everything targeted is
        confirmed gone.
    """
    if pid == os.getpid():
        # Only reachable from a fabricated or wildly recycled record, and the
        # one target that would take the caller down with it.
        return []
    snapshot = descendants(pid)
    rooted = snapshot.get(pid)
    if rooted is None:
        return []
    if (
        start_time is not None
        and rooted.start_time is not None
        and rooted.start_time != start_time
    ):
        return []
    _signal_processes(snapshot, signal.SIGTERM)
    _await_exit(snapshot, grace_s)
    remaining = _rescan(snapshot)
    if not remaining:
        return []
    _signal_processes(remaining, signal.SIGKILL)
    _await_exit(remaining, confirm_s)
    return sorted(_alive_targets(remaining))


def worker_python() -> str:
    """Return the interpreter the detached worker runs under."""
    override = os.environ.get(WORKER_PYTHON_VARIABLE, "").strip()
    return override or sys.executable


def session_id() -> str | None:
    """Return the originating Claude session id, or None when absent."""
    value = os.environ.get(SESSION_ID_VARIABLE, "").strip()
    return value or None


def state_root() -> Path:
    """Return the root every per-workspace state dir lives under.

    ``CLAUDE_PLUGIN_DATA`` wins when set, else ``XDG_STATE_HOME`` as the
    bare-CLI fallback. An EMPTY or RELATIVE value for either counts as unset: a
    relative root would resolve differently in the dispatcher and in the worker,
    which runs from a different cwd.
    """
    plugin_data = _absolute_env_dir("CLAUDE_PLUGIN_DATA")
    if plugin_data is not None:
        return plugin_data / "state"
    xdg = _absolute_env_dir("XDG_STATE_HOME")
    if xdg is not None:
        return xdg / "chinamax"
    return Path.home() / ".local" / "state" / "chinamax"


#: Comms of the transient wrappers between a hook's python and the long-lived
#: Claude process: the login shells and the plugin's own `scripts/` shims. The
#: ancestor walk skips them so the registered pid is Claude's, never a runner's.
_SHELL_COMMS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_SHIM_COMMS = frozenset(
    {
        "chinamax",
        "session_start_hook",
        "session_end_hook",
        "stop_hook",
        "user_prompt_hook",
        "bridge_contract_hook",
        "conda",
        "env",
    }
)


def sessions_dir() -> Path:
    """Return the session-liveness registry directory.

    A SIBLING of the state root (``<data root>/sessions``), deliberately OUTSIDE
    it so `iter_workspace_stores`'s ``state_root()/*`` walk never mistakes a
    registry file for a workspace store.
    """
    return state_root().parent / "sessions"


def session_registry_path(session_id_value: str) -> Path:
    """Return one session's registry file path."""
    return sessions_dir() / session_id_value


def read_comm(pid: int) -> str | None:
    """Return a process's command name (``/proc/<pid>/comm``), or None."""
    try:
        return (
            Path(f"/proc/{pid}/comm")
            .read_text(encoding="utf-8", errors="replace")
            .strip()
        )
    except OSError:
        return None


def resolve_owner_process(start_pid: int | None = None) -> tuple[int, int | None] | None:
    """Return the long-lived Claude process ``(pid, start_time)``, or None.

    A hook runs as a grandchild subprocess of its Claude session, so the Claude
    pid must be recovered by climbing the ``/proc`` ancestor chain: from
    ``os.getppid()`` upward, skipping ancestors whose comm is a login shell or a
    plugin ``scripts/`` shim, take the first remaining ancestor. Recording a
    transient runner pid instead would make every session read dead and turn the
    orphan reaper into a live-Job killer.

    Args:
        start_pid: Where to start the walk; ``os.getppid()`` by default.

    Returns:
        The Claude process ``(pid, start_time)``, or None when nothing resolves
        (a non-Linux dev box with no ``/proc``).
    """
    seen: set[int] = set()
    pid = os.getppid() if start_pid is None else start_pid
    fallback: tuple[int, int | None] | None = None
    while pid > 1 and pid not in seen:
        seen.add(pid)
        info = read_process(pid)
        if info is None:
            break
        candidate = (pid, info.start_time)
        comm = (read_comm(pid) or "").lower()
        if comm not in _SHELL_COMMS and comm not in _SHIM_COMMS:
            return candidate
        # Every ancestor is a shell/shim so far — keep the highest as a fallback,
        # so a session is still registered rather than dropped.
        fallback = candidate
        pid = info.ppid
    return fallback


def session_alive(pid: int | None, start_time: int | None) -> bool:
    """Report whether a registered session's Claude process is still alive.

    Reuses the `worker_gone` predicate family — ESRCH-only gone, EPERM alive,
    ``/proc`` state ``Z`` gone, start-time mismatch gone — NOT a bare
    ``os.kill(pid, 0)``, which reads a zombie or a recycled pid as alive and
    would leave a dead session's orphans running forever.

    Args:
        pid: The registered Claude pid, or None.
        start_time: Its recorded ``/proc`` start time, or None.

    Returns:
        Whether the session's process is still that same live process.
    """
    if pid is None:
        return False
    return not worker_gone({"pid": pid, "pidStartTime": start_time})


def write_session_registry(
    session_id_value: str, pid: int, start_time: int | None
) -> None:
    """Record a live Claude session's ``(pid, start_time)`` atomically."""
    path = session_registry_path(session_id_value)
    make_dir(path.parent)
    _write_file(
        path,
        json.dumps({"pid": pid, "pidStartTime": start_time}, sort_keys=True) + "\n",
    )


def read_session_registry(session_id_value: str) -> tuple[int, int | None] | None:
    """Return a registered session's ``(pid, start_time)``, or None when absent."""
    try:
        data = json.loads(
            session_registry_path(session_id_value).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
        return None
    start = data.get("pidStartTime")
    return data["pid"], start if isinstance(start, int) else None


def remove_session_registry(session_id_value: str) -> None:
    """Remove one session's registry file, tolerating absence."""
    _unlink(session_registry_path(session_id_value))


def iter_session_registrations() -> Iterator[tuple[str, int, int | None]]:
    """Yield every ``(session_id, pid, start_time)`` in the registry."""
    try:
        entries = list(os.scandir(sessions_dir()))
    except OSError:
        return
    for entry in sorted(entries, key=lambda item: item.name):
        if not entry.is_file():
            continue
        registration = read_session_registry(entry.name)
        if registration is not None:
            yield entry.name, registration[0], registration[1]


def session_is_alive(session_id_value: str) -> bool:
    """Report whether a session's registered Claude process is still alive."""
    registration = read_session_registry(session_id_value)
    if registration is None:
        return False
    return session_alive(registration[0], registration[1])


def resolve_workspace_root(workspace: str | Path | None = None) -> Path:
    """Resolve the workspace root a Job's state is keyed on.

    ``--workspace`` when given, else the cwd, resolved to the git toplevel when
    that succeeds — so dispatching from a subdirectory does not fragment one
    repo's state across several dirs.

    Args:
        workspace: The requested workspace, or None for the cwd.

    Returns:
        The resolved root: the git toplevel, else the directory's realpath.

    Raises:
        ChinamaxError: If the requested workspace is not an existing directory.
    """
    candidate = Path.cwd() if workspace is None else Path(workspace)
    if not candidate.is_dir():
        raise ChinamaxError(f"workspace must name an existing directory: {candidate}")
    resolved = Path(os.path.realpath(candidate))
    toplevel = _git_toplevel(resolved)
    return resolved if toplevel is None else toplevel


def workspace_key(root: Path) -> str:
    """Return the per-workspace directory name ``<repo-basename>-<sha256[:16]>``."""
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return f"{root.name or 'root'}-{digest}"


def open_store(workspace: str | Path | None = None) -> "JobStore":
    """Open the store for one workspace, resolving its root first.

    Args:
        workspace: The requested workspace, or None for the cwd.

    Returns:
        The store, carrying the RESOLVED workspace root.

    Raises:
        ChinamaxError: If the requested workspace is not an existing directory.
    """
    root = resolve_workspace_root(workspace)
    return JobStore(state_root() / workspace_key(root), workspace_root=root)


def list_jobs_tolerant(workspace: str | Path | None = None) -> tuple[list[dict], int]:
    """Enumerate a workspace's Jobs, tolerating malformed or missing state.

    THE shared tolerant enumeration seam behind both session hooks and the same
    ``JobStore.load_records`` path ``status`` reads: a malformed ``jobs/<id>.json``
    is skipped and only counted, never allowed to hide the healthy Jobs beside it,
    and a missing or truncated ``state.json`` is rebuilt from the record filenames
    rather than treated as an error (jobs/01's store-invariants decision). The
    hooks want the COUNT of unparseable records, not their ids, so that is what
    this returns.

    Args:
        workspace: The requested workspace, or None for the cwd; resolved to the
            git toplevel like every other verb.

    Returns:
        The readable records, and the number that would not parse.
    """
    records, malformed = open_store(workspace).load_records()
    return records, len(malformed)


def iter_workspace_stores() -> Iterator["JobStore"]:
    """Yield a JobStore for every per-workspace state dir under the state root.

    THE shared reap iteration: a session may own Jobs in several workspaces
    (``task --workspace``), so both reap forms sweep every store rather than only
    the cwd's. The `sessions/` registry sits OUTSIDE ``state_root()`` and the
    ``python-path`` record is a file, so a directory-only walk keeps both out of
    the sweep by construction.
    """
    try:
        entries = sorted(state_root().glob("*"))
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            yield JobStore(entry)


@dataclass
class ReapResult:
    """The outcome of one reap sweep.

    ``reaped`` are the records marked terminal; ``survived`` are ``(id, pids)``
    for Jobs whose worker outlived the SIGKILL sweep — left for the next pass,
    exactly as `cancel` refuses to write ``cancelled`` over a Job it did not
    stop; ``reported`` are live workers with no recorded owner, surfaced but
    never killed (a missing ``sessionId`` is a plumbing failure, not a kill path).
    """

    reaped: list[dict]
    survived: list[tuple[str, list[int]]]
    reported: list[dict]


def _stored_active_records(store: "JobStore") -> list[dict]:
    """Return a store's records whose STORED status is ``queued`` or ``running``.

    Deliberately NOT `is_active`: a crashed worker's record (stored ``running``,
    derived ``interrupted``) must still be reaped and marked, or it stays
    resumable from a later session in violation of ADR 0004.
    """
    records, _ = store.load_records()
    return [record for record in records if record.get("status") in ACTIVE_STATUSES]


def _reap_record(
    store: "JobStore",
    record: dict,
    terminal_status: str,
    reason: str,
    grace_s: float,
    confirm_s: float,
) -> tuple[dict | None, list[int]]:
    """Kill a live worker's tree (if any) and mark the record terminal via CAS.

    A live worker is killed first; a provably-gone one is marked WITHOUT a kill.
    If the tree outlives SIGKILL the terminal status is NOT written — the
    survivors are returned and the record is left for the next sweep.

    Returns:
        ``(marked_record_or_None, survivor_pids)``.
    """
    pid = usable_pid(record)
    if pid is not None and not worker_gone(record):
        survivors = terminate_tree(
            pid, record.get("pidStartTime"), grace_s=grace_s, confirm_s=confirm_s
        )
        if survivors:
            return None, survivors
    marked = store.update(
        record["id"],
        {"status": terminal_status, "completedAt": utc_now(), "errorMessage": reason},
        expect=ACTIVE_STATUSES,
    )
    return marked, []


def reap_session(
    session_id_value: str,
    *,
    grace_s: float = SESSION_REAP_GRACE_S,
    confirm_s: float = SESSION_REAP_CONFIRM_S,
) -> ReapResult:
    """Reap every stored-active Job a session owns: kill the tree, mark cancelled.

    The SessionEnd path (and the same-PID predecessor reap). Every workspace
    store is swept; a Job whose worker is already gone is marked without a kill.

    Args:
        session_id_value: The ending session's id.
        grace_s: SIGTERM grace for each kill.
        confirm_s: SIGKILL-confirm bound for each kill.

    Returns:
        The `ReapResult`.
    """
    result = ReapResult([], [], [])
    for store in iter_workspace_stores():
        for record in _stored_active_records(store):
            if record.get("sessionId") != session_id_value:
                continue
            marked, survivors = _reap_record(
                store, record, STATUS_CANCELLED, SESSION_REAP_REASON, grace_s, confirm_s
            )
            if survivors:
                result.survived.append((record["id"], survivors))
            elif marked is not None:
                result.reaped.append(marked)
    return result


def reap_orphans(
    *,
    grace_s: float = SESSION_REAP_GRACE_S,
    confirm_s: float = SESSION_REAP_CONFIRM_S,
) -> ReapResult:
    """Mark `interrupted` every stored-active Job whose owner session is dead.

    The SessionStart crash-recovery path (and the CLI `reap --orphans`). "Owner
    dead" = a `sessionId` whose registry says the Claude process is gone, or a
    registry missing entirely. A record with NO `sessionId` is reaped ONLY when
    its worker is already provably gone; a LIVE worker with no recorded owner is
    reported, never killed. Stale registry files (whose process is gone) are then
    unlinked.

    Returns:
        The `ReapResult` — ``reported`` holds the ownerless live workers.
    """
    result = ReapResult([], [], [])
    for store in iter_workspace_stores():
        for record in _stored_active_records(store):
            sid = record.get("sessionId")
            if sid is not None and session_is_alive(sid):
                continue
            if sid is None and not worker_gone(record):
                result.reported.append(record)
                continue
            marked, survivors = _reap_record(
                store, record, STATUS_INTERRUPTED, ORPHAN_REAP_REASON, grace_s, confirm_s
            )
            if survivors:
                result.survived.append((record["id"], survivors))
            elif marked is not None:
                result.reaped.append(marked)
    _gc_stale_registrations()
    return result


def _gc_stale_registrations() -> None:
    """Unlink every registry file whose recorded Claude process is gone."""
    for session_id_value, pid, start_time in list(iter_session_registrations()):
        if not session_alive(pid, start_time):
            remove_session_registry(session_id_value)


def make_dir(path: Path) -> Path:
    """Create a state directory 0700, tolerating an existing one."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def precreate(path: Path) -> Path:
    """Create a state file 0600 without truncating it, and return its path.

    The Runtime opens ``transcript_path`` and ``result_path`` itself, and
    ordinary Python creation under the default umask yields 0644 — so the worker
    precreates both before handing the spec over.

    Args:
        path: The file to create if absent.

    Returns:
        The same path.
    """
    make_dir(path.parent)
    handle = os.open(path, os.O_CREAT | os.O_WRONLY, FILE_MODE)
    try:
        os.fchmod(handle, FILE_MODE)
    finally:
        os.close(handle)
    return path


def secure_file(path: Path) -> None:
    """Re-apply 0600 to a file another writer may have replaced.

    The Runtime writes ``result_path`` with tmp+rename, and the renamed
    temporary carries its own mode rather than the precreated one.
    """
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def log_signature(path: Path) -> tuple[int, int]:
    """Return a log's ``(size, inode)``.

    Watching SIZE is what keeps a long single phase from going silent for a
    whole `--wait` window, and it is O(1) per poll where recounting lines would
    rescan a growing file every two seconds.
    """
    try:
        info = path.stat()
    except OSError:
        return (-1, -1)
    return (info.st_size, info.st_ino)


def tail_lines(path: Path, count: int) -> list[str]:
    """Return the last ``count`` non-empty lines of a log.

    Read by seeking the tail rather than loading a 70-minute log whole, and
    decoded ``errors="replace"`` since tool output is arbitrary bytes.

    Args:
        path: The log path.
        count: How many lines to keep.

    Returns:
        The trailing non-empty lines, oldest first; empty when unreadable.
    """
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _TAIL_WINDOW_BYTES))
            blob = stream.read()
    except OSError:
        return []
    lines = [line for line in blob.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return lines[-count:] if count > 0 else []


def new_job_id(clock: Callable[[], float] = time.time) -> str:
    """Mint a Job id: ``task-<base36 ms>-<6 random>``."""
    suffix = "".join(random.choices(_ID_ALPHABET, k=6))
    return f"task-{_base36(int(clock() * 1000))}-{suffix}"


def steer_now_ms() -> int:
    """Return the epoch-ms a steer write stamps into its queue-file name.

    Honours `STEER_CLOCK_VARIABLE` when set, so a test can pin two writes to one
    millisecond; falls back to the wall clock otherwise.
    """
    override = os.environ.get(STEER_CLOCK_VARIABLE, "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return int(time.time() * 1000)


def _next_steer_seq() -> int:
    """Return the next per-process steer sequence number."""
    global _steer_seq_counter
    with _steer_seq_lock:
        value = _steer_seq_counter
        _steer_seq_counter += 1
        return value


def _steer_filename(now_ms: int, seq: int) -> str:
    """Build a steer's queue-file name ``<epoch-ms>-<seq>-<6 random>.md``.

    Every component earns its place: ``epoch-ms`` is fixed-width and orders
    writes across processes, ``seq`` (zero-padded ``%04d``) orders a same-process
    burst inside one millisecond, and the random suffix makes the name unique
    outright — two `steer` processes landing in the same millisecond cannot
    collide, which is what lets the write stay a plain atomic tmp+rename.
    """
    suffix = "".join(random.choices(_ID_ALPHABET, k=6))
    return f"{now_ms}-{seq:04d}-{suffix}.md"


def write_steer(steer_dir: Path, message: str) -> str:
    """Enqueue one steer as an ordered, uniquely-named file, and return its id.

    The name is unique by construction, so the write is a plain atomic tmp+rename
    through `_write_file` — NO exclusive-create dance (``os.link`` strands the tmp
    inode; an in-place ``O_CREAT|O_EXCL`` exposes a half-written file to a
    concurrent drain) and NO name derived by re-listing the directory (which can
    reuse an id whose same-millisecond predecessor already moved to ``consumed/``,
    so the drain's dedupe would silently swallow the NEW steer). ``_write_file``'s
    temporary is ``.<name>.<rand>.tmp`` — no ``.md`` suffix, so the drain's glob
    skips it.

    Args:
        steer_dir: The Job's steer queue directory.
        message: The steer text, stored verbatim (the marker is added at drain).

    Returns:
        The steer id: the queue-file name.
    """
    make_dir(steer_dir)
    name = _steer_filename(steer_now_ms(), _next_steer_seq())
    _write_file(steer_dir / name, message)
    return name


def list_steer_files(steer_dir: Path) -> list[Path]:
    """Return the queued (undrained) steer files, in filename order.

    Lists ``*.md`` files only: the ``consumed/`` subdirectory and the drain's
    own ``.tmp`` temporaries are both skipped without a special case.

    Args:
        steer_dir: The Job's steer queue directory.

    Returns:
        The queued steer files, sorted by name (write order).
    """
    try:
        entries = list(os.scandir(steer_dir))
    except OSError:
        return []
    files = [
        Path(entry.path)
        for entry in entries
        if entry.is_file() and entry.name.endswith(".md")
    ]
    return sorted(files, key=lambda path: path.name)


def steer_enqueued_at(steer_id: str) -> str:
    """Recover a steer's enqueue timestamp from its id's leading epoch-ms.

    The id's first component IS the enqueue millisecond, so the timestamp needs
    no separate storage. A malformed id falls back to the current time rather
    than failing a drain.
    """
    head = steer_id.split("-", 1)[0]
    try:
        return datetime.fromtimestamp(int(head) / 1000, timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return utc_now()


def new_record(
    job_id: str,
    *,
    prompt: str,
    profile: str,
    write: bool,
    workspace_root: Path,
    log_file: Path,
    bash_timeout_s: float | None = None,
    originating_session: str | None = None,
    bridge_name: str | None = None,
) -> dict:
    """Build the first version of a Job record.

    ``request`` is exactly what the worker needs to rehydrate without seeing the
    dispatcher's argv.

    Args:
        job_id: The reserved id.
        prompt: The Job's prompt.
        profile: The resolved Profile name.
        write: Whether the Job is write-capable.
        workspace_root: The RESOLVED workspace root, which the worker's spec
            pins bash to — it must not diverge from the state-dir key.
        log_file: The Job's progress log path.
        bash_timeout_s: The per-command bash timeout, when overridden.
        originating_session: The owning Claude session id, or None.
        bridge_name: The persistent Bridge teammate that owns this Job's Thread
            (``chinamax-<profile>-<task-slug>``), or None for a direct dispatch.

    Returns:
        The record, ready to publish.
    """
    now = utc_now()
    request: dict = {
        "prompt": prompt,
        "profile": profile,
        "write": write,
        "workspaceRoot": str(workspace_root),
    }
    if bash_timeout_s is not None:
        request["bashTimeoutSec"] = bash_timeout_s
    record = dict(RECORD_DEFAULTS)
    record.update(
        {
            "id": job_id,
            "title": summarize(prompt),
            "profile": profile,
            "write": write,
            "workspaceRoot": str(workspace_root),
            "sessionId": originating_session,
            "bridgeName": bridge_name,
            "status": STATUS_QUEUED,
            "createdAt": now,
            "updatedAt": now,
            "logFile": str(log_file),
            "request": request,
        }
    )
    return record


def resolve_selector(selector: str, known: Iterable[str]) -> str:
    """Resolve an exact id or an unambiguous prefix against a known id set.

    THE shared resolver: `JobStore.resolve_job` is a thin wrapper around it and
    every verb goes through one of the two, so `result`/`cancel`/`resume` can
    never drift from `status`/`logs`.

    Args:
        selector: The id or prefix the operator named.
        known: Every known Job id.

    Returns:
        The matching Job id.

    Raises:
        ChinamaxError: On no match or an ambiguous prefix — including an
            explicit id against an empty store, which is never answered with a
            silent success.
    """
    candidates = sorted(known)
    if selector in candidates:
        return selector
    matches = [job_id for job_id in candidates if job_id.startswith(selector)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ChinamaxError(
            f"ambiguous Job selector {selector!r}; candidates: {', '.join(matches)}"
        )
    listed = ", ".join(candidates) if candidates else "(none)"
    raise ChinamaxError(f"no Job matching {selector!r}; known Jobs: {listed}")


def normalize_record(record: dict) -> dict:
    """Fill in missing optional fields, keeping every unknown one.

    That pair is what makes the schema additive: no field is ever removed or
    repurposed, and a record written by a newer plugin still loads here.

    Args:
        record: The decoded record.

    Returns:
        The record with defaults applied.
    """
    merged = dict(RECORD_DEFAULTS)
    merged.update(record)
    return merged


class JobStore:
    """One workspace's durable Job store."""

    def __init__(self, path: str | Path, workspace_root: Path | None = None) -> None:
        """Bind a store to its per-workspace directory.

        Args:
            path: The per-workspace state directory.
            workspace_root: The resolved workspace root, when known.
        """
        self.path = Path(path)
        self.workspace_root = workspace_root

    @property
    def jobs_dir(self) -> Path:
        """Return the directory holding every per-Job artifact."""
        return self.path / "jobs"

    @property
    def index_path(self) -> Path:
        """Return the derived id index."""
        return self.path / "state.json"

    @property
    def lock_path(self) -> Path:
        """Return the DEDICATED lock sidecar — never ``state.json`` itself."""
        return self.path / "state.lock"

    def record_path(self, job_id: str) -> Path:
        """Return a Job's record path."""
        return self.jobs_dir / f"{job_id}.json"

    def log_path(self, job_id: str) -> Path:
        """Return a Job's timestamped progress log."""
        return self.jobs_dir / f"{job_id}.log"

    def spawn_log_path(self, job_id: str) -> Path:
        """Return a Job's spawn log: worker stdio before the loop owns logging."""
        return self.jobs_dir / f"{job_id}.spawn.log"

    def transcript_path(self, job_id: str) -> Path:
        """Return a Job's Thread transcript."""
        return self.jobs_dir / f"{job_id}.thread.jsonl"

    def result_path(self, job_id: str) -> Path:
        """Return a Job's verbatim Runtime result artifact."""
        return self.jobs_dir / f"{job_id}.result.json"

    def steer_dir(self, job_id: str) -> Path:
        """Return a Job's steer queue directory (drained in jobs/03)."""
        return self.jobs_dir / f"{job_id}.steer"

    def enqueue_steer(self, job_id: str, message: str) -> str:
        """Append one steer to a Job's queue and return its id (the file name)."""
        return write_steer(self.steer_dir(job_id), message)

    def ensure(self) -> "JobStore":
        """Create the store's directories 0700."""
        make_dir(self.path)
        make_dir(self.jobs_dir)
        return self

    def reserve_id(self) -> str:
        """Reserve a unique Job id by ``O_EXCL``-creating its record path.

        Id uniqueness is this atomic creation and nothing else; every later
        write to the record is tmp+rename.

        Returns:
            The reserved id.
        """
        self.ensure()
        while True:
            job_id = new_job_id()
            try:
                handle = os.open(
                    self.record_path(job_id),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    FILE_MODE,
                )
            except FileExistsError:
                continue
            try:
                os.fchmod(handle, FILE_MODE)
            finally:
                os.close(handle)
            return job_id

    def create(self, record: dict) -> dict:
        """Publish the first version of a reserved record, then the index.

        Args:
            record: The record to publish.

        Returns:
            The published record.
        """
        with self._locked():
            self._publish(record)
            self._sync_index()
        return record

    def update(
        self,
        job_id: str,
        changes: dict | None = None,
        *,
        expect: frozenset[str] | set[str] | None = None,
        touch: bool = True,
    ) -> dict | None:
        """Apply one compare-and-swap change to a record.

        The single mutation path: it takes ``state.lock``, re-reads the record,
        applies the change only if the record is still in the expected state,
        and publishes it by tmp+rename. Compare-and-swap rather than
        last-writer-wins is what stops a late heartbeat from resurrecting
        ``running`` over a terminal write, and what makes jobs/02's cancel
        un-outrunnable.

        Args:
            job_id: The Job to mutate.
            changes: The fields to set.
            expect: The statuses the change applies to; None applies always.
            touch: Whether to refresh ``updatedAt``.

        Returns:
            The published record, or None when the record was unreadable or the
            compare-and-swap failed.
        """
        with self._locked():
            record = self._read(job_id)
            if record is None:
                return None
            if expect is not None and record.get("status") not in expect:
                return None
            if changes:
                record.update(changes)
            if touch:
                record["updatedAt"] = utc_now()
            self._publish(record)
            self._sync_index()
            return normalize_record(record)

    def claim(self, job_id: str, changes: dict) -> dict | None:
        """Claim a queued record, or RECLAIM a crashed worker's non-terminal one.

        The one edit jobs/03 makes to jobs/01's claim CAS, and the only caller is
        ``task-worker`` startup — no verb path reaches it. A ``queued`` record is
        claimed as before. The reclaim arm additionally admits a NON-terminal
        record (``queued`` or ``running``) whose recorded worker is PROVABLY GONE
        by `worker_gone` — deliberately WITHOUT `effective_status`'s 60 s stale
        grace, since an explicit relaunch verifies liveness directly rather than
        guarding against a delayed heartbeat. A live claimant, or a terminal
        record (a cancel that landed first), still yields None and exit-without-
        running. Runs inside the one locked compare-and-swap, so a second worker
        racing the same record cannot both win.

        Args:
            job_id: The Job to claim.
            changes: The claim's fields (status/pid/pidStartTime/startedAt/phase).

        Returns:
            The published record, or None when the record is terminal, held by a
            live claimant, or unreadable.
        """
        with self._locked():
            record = self._read(job_id)
            if record is None:
                return None
            status = record.get("status")
            if status != STATUS_QUEUED and not (
                status in ACTIVE_STATUSES and worker_gone(normalize_record(record))
            ):
                return None
            record.update(changes)
            record["updatedAt"] = utc_now()
            self._publish(record)
            self._sync_index()
            return normalize_record(record)

    def try_read(self, job_id: str) -> dict | None:
        """Return one record, or None when it is missing, empty or unparsable."""
        record = self._read(job_id)
        return None if record is None else normalize_record(record)

    def read(self, job_id: str) -> dict:
        """Return one record.

        Args:
            job_id: The Job to read.

        Returns:
            The record, with defaults applied for missing optional fields.

        Raises:
            ChinamaxError: If the record is missing, empty or unparsable.
        """
        record = self.try_read(job_id)
        if record is None:
            raise ChinamaxError(f"no readable Job record for {job_id}")
        return record

    def job_ids(self) -> list[str]:
        """Return every Job id, rebuilding the index when it disagrees.

        The equality check compares the id SET derived from FILENAMES, never
        record contents — the index exists so reads do not open every record.

        Returns:
            The ids, sorted.
        """
        derived = self._derived_ids()
        if self._stored_ids() != derived:
            # The rebuild is a WRITE, so it obeys the same locking discipline as
            # every other write: an unlocked rebuild racing a mutator's
            # record-then-index pair would put a stale index over a fresh one.
            with self._locked():
                derived = self._sync_index()
        return sorted(derived)

    def load_records(self) -> tuple[list[dict], list[str]]:
        """Return every readable record, plus the ids that would not parse.

        A record that is empty or unparsable — including one caught in the
        window between the ``O_EXCL`` reservation and its first publish — is
        reported rather than silently dropped, and never allowed to hide the
        healthy Jobs beside it.

        Returns:
            The records, and the malformed ids.
        """
        records: list[dict] = []
        malformed: list[str] = []
        for job_id in self.job_ids():
            record = self.try_read(job_id)
            if record is None:
                malformed.append(job_id)
            else:
                records.append(record)
        return records, malformed

    def resolve_job(self, selector: str) -> str:
        """Resolve an exact id or an unambiguous prefix against this store.

        Args:
            selector: The id or prefix the operator named.

        Returns:
            The matching Job id.

        Raises:
            ChinamaxError: On no match or an ambiguous prefix.
        """
        return resolve_selector(selector, self.job_ids())

    def prune(self, keep: int = PRUNE_KEEP) -> list[str]:
        """Drop the oldest finished Jobs beyond ``keep``, artifacts and all.

        Called on dispatch and on terminal transitions only — never on a
        throttled progress update or a steer write, which would scan-and-unlink
        constantly and widen the window where a concurrent `logs`/`result` read
        hits a half-removed Job.

        Args:
            keep: How many finished Jobs to keep.

        Returns:
            The ids that were dropped.
        """
        with self._locked():
            return self._prune_locked(keep)

    def create_resume(
        self, selector: str | None, prompt: str, *, originating_session: str | None = None
    ) -> dict:
        """Create a Job continuing a finished Job's Thread.

        The guard, the source resolution, the transcript copy and the new
        record's creation all happen inside ONE critical section: creating the
        record under the lock but copying after it would let a concurrent prune
        drop the source in between, and two concurrent resumes could otherwise
        both observe "nothing active" and both dispatch.

        The DISPATCHER writes the copy, never the worker — a worker only ever
        reads its own transcript, so nothing can pull the source out from under
        a rehydrating Job. The new Job inherits the source's Profile, write
        posture, workspace root, bash timeout AND ``bridgeName``: a resume must
        not silently change posture or provider, and one Bridge owns one Thread
        lineage (ADR 0006), so the roster and bridge-first status stay populated
        across resumes. It also records ``resumedFrom`` (the source id) and a
        persisted ``lineageRoot`` (the source's ``lineageRoot``, else the source
        id) — the lineage-scoped resume guard compares ``lineageRoot`` values
        directly rather than walking ``resumedFrom`` chains, so a pruned ancestor
        or a corrupt cycle can neither break nor hang it. Pending steers are
        deliberately NOT carried forward — the new Job has its own empty queue.

        The refusal is lineage-scoped for an explicit selector (refuse only when
        this Thread's own lineage still has an active Job — an unrelated active
        Job is fine) and workspace-wide for a bare resume (which must guess a
        target). A source whose STORED status is `interrupted` (a reaped
        dead-session Job) is refused by both forms in `_resume_source`.

        Args:
            selector: The source id or prefix, or None for the most recent
                non-active Job with a Thread.
            prompt: The follow-up, already resolved (never empty).
            originating_session: The CURRENT session id — the owning session of
                the new Job.

        Returns:
            The new Job's record.

        Raises:
            ChinamaxError: If the lineage (explicit) or any Job (bare) is still
                active, the source cannot be resolved or was reaped, or its
                Thread is absent, empty or unusable.
        """
        with self._locked():
            records = self._load_locked()
            if selector is None:
                # A bare resume must guess a target, so it keeps the workspace-wide
                # refusal — and it must fire BEFORE resolving a source, so an active
                # but thread-less Job reports "still running", not "nothing to
                # resume".
                running = sorted(record["id"] for record in records if is_active(record))
                if running:
                    raise ChinamaxError(
                        "a Job is still running — use `status` to follow it or "
                        f"`cancel` to stop it first: {', '.join(running)}"
                    )
                source = self._resume_source(None, records)
            else:
                source = self._resume_source(selector, records)
                lineage_root = source.get("lineageRoot") or source["id"]
                # Explicit selector: refuse only when this Thread's OWN lineage
                # still has an active Job — an unrelated active Job is fine.
                active_lineage = sorted(
                    other["id"]
                    for other in records
                    if is_active(other)
                    and (other.get("lineageRoot") or other["id"]) == lineage_root
                )
                if active_lineage:
                    raise ChinamaxError(
                        f"{source['id']} lineage still running — use status"
                    )
            lineage_root = source.get("lineageRoot") or source["id"]
            messages = merge_follow_up(self._resume_thread(source["id"]), prompt)
            request = source.get("request") or {}
            job_id = self.reserve_id()
            record = new_record(
                job_id,
                prompt=prompt,
                profile=request.get("profile") or source.get("profile"),
                write=bool(request.get("write", source.get("write", True))),
                workspace_root=Path(
                    request.get("workspaceRoot")
                    or source.get("workspaceRoot")
                    or self.workspace_root
                ),
                log_file=self.log_path(job_id),
                bash_timeout_s=request.get("bashTimeoutSec"),
                originating_session=originating_session,
                bridge_name=source.get("bridgeName"),
            )
            record["resumedFrom"] = source["id"]
            record["lineageRoot"] = lineage_root
            write_messages(precreate(self.transcript_path(job_id)), messages)
            self._publish(record)
            self._sync_index()
            return normalize_record(record)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold ``state.lock`` for one mutation or rebuild."""
        self.ensure()
        handle = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, FILE_MODE)
        try:
            os.fchmod(handle, FILE_MODE)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            os.close(handle)

    def _publish(self, record: dict) -> None:
        """Write one record atomically. The caller holds the lock."""
        _write_file(
            self.record_path(record["id"]),
            json.dumps(record, indent=2, sort_keys=True) + "\n",
        )

    def _read(self, job_id: str) -> dict | None:
        """Read one record raw, without applying defaults."""
        try:
            text = self.record_path(job_id).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _derived_ids(self) -> set[str]:
        """Return the id set the record FILENAMES imply."""
        try:
            entries = list(os.scandir(self.jobs_dir))
        except OSError:
            return set()
        found = set()
        for entry in entries:
            match = _RECORD_RE.match(entry.name)
            if match is not None:
                found.add(match.group(1))
        return found

    def _stored_ids(self) -> set[str] | None:
        """Return the index's id set, or None when it is missing or unusable."""
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        ids = data.get("jobs")
        if not isinstance(ids, list) or not all(isinstance(one, str) for one in ids):
            return None
        return set(ids)

    def _sync_index(self) -> set[str]:
        """Rebuild the index when it disagrees with the records.

        The caller holds the lock. Mutation order is always record-then-index.
        """
        derived = self._derived_ids()
        if self._stored_ids() != derived:
            _write_file(
                self.index_path,
                json.dumps(
                    {"schemaVersion": SCHEMA_VERSION, "jobs": sorted(derived)}, indent=2
                )
                + "\n",
            )
        return derived

    def _load_locked(self) -> list[dict]:
        """Return every readable record. The caller holds the lock.

        Deliberately not `load_records`: that one takes the lock through
        `job_ids`, and `flock` on a second descriptor from the same process
        blocks rather than nesting.
        """
        records = []
        for job_id in sorted(self._derived_ids()):
            raw = self._read(job_id)
            if raw is not None:
                records.append(normalize_record(raw))
        return records

    def _resume_source(self, selector: str | None, records: list[dict]) -> dict:
        """Pick the Job whose Thread a resume continues. The caller holds the lock.

        A source whose STORED status is `interrupted` (a reaped dead-session Job)
        is refused by BOTH forms — ADR 0004's "dead-session Job ids are never
        resumed" made mechanism, not convention. A DERIVED-interrupted crash
        (stored ``running``/``queued``) stays resumable, as before.
        """
        by_id = {record["id"]: record for record in records}
        if selector is not None:
            source = by_id[resolve_selector(selector, by_id)]
            if source.get("status") == STATUS_INTERRUPTED:
                raise ChinamaxError(
                    f"{source['id']} cannot be resumed: its owning Claude session "
                    "ended and the Job was reaped"
                )
            return source
        with_thread = [
            record
            for record in records
            if not is_active(record)
            and record.get("status") != STATUS_INTERRUPTED
            and self._thread_size(record["id"]) > 0
        ]
        if not with_thread:
            raise ChinamaxError(
                "no finished Job with a Thread to resume in this workspace"
            )
        return max(with_thread, key=latest_key)

    def _resume_thread(self, job_id: str) -> list[dict]:
        """Read and repair one Job's Thread for a resume. The caller holds the lock."""
        path = self.transcript_path(job_id)
        if self._thread_size(job_id) <= 0:
            raise ChinamaxError(
                f"{job_id} has no Thread to resume: {path} is absent or empty"
            )
        try:
            messages = read_repaired_messages(path)
        except (OSError, ValueError) as error:
            raise ChinamaxError(f"{job_id}'s Thread is unreadable: {error}") from error
        if not messages:
            raise ChinamaxError(
                f"{job_id}'s Thread yields no usable turn after normalization: {path}"
            )
        return messages

    def _thread_size(self, job_id: str) -> int:
        """Return a Job's transcript size, or 0 when it is absent."""
        try:
            return self.transcript_path(job_id).stat().st_size
        except OSError:
            return 0

    def _prune_locked(self, keep: int) -> list[str]:
        """Drop the oldest finished Jobs beyond ``keep``. The caller holds the lock.

        Grades the STORED status, NOT `effective_status`: a reaped
        STORED-`interrupted` record IS finished and prunable, while a DERIVED-
        interrupted crash (stored ``running``/``queued``) is NOT — its resumable
        Thread is exactly what pruning it would destroy. Equivalent to the old
        `effective_status` grading for every other status, since derivation only
        ever produced `interrupted`, which was never graded finished.
        """
        finished = [
            record
            for record in self._load_locked()
            if record["status"] in FINISHED_STATUSES
        ]
        finished.sort(key=latest_key, reverse=True)
        dropped = [record["id"] for record in finished[keep:]]
        for job_id in dropped:
            self._drop_job(job_id)
        if dropped:
            self._sync_index()
        return dropped

    def _drop_job(self, job_id: str) -> None:
        """Remove every artifact of one Job. The caller holds the lock.

        The record `.json` goes FIRST: it is what makes a Job resolvable, so a
        concurrent `logs`/`result` either resolved the Job before this began or
        cannot see it at all, instead of resolving it and then reading a
        half-removed set. All SIX artifacts of the authoritative layout are
        covered — the `.steer/` queue recursively, since a consumed/
        subdirectory nests inside it and a flat unlink loop leaves it behind.
        Missing files are tolerated so this is idempotent after a crash, and
        symlinks are never followed.
        """
        _unlink(self.record_path(job_id))
        for path in (
            self.log_path(job_id),
            self.spawn_log_path(job_id),
            self.transcript_path(job_id),
            self.result_path(job_id),
        ):
            _unlink(path)
        _unlink_tree(self.steer_dir(job_id))


class ProgressReporter:
    """Mirrors the loop's progress into ``jobs/<id>.log`` and the Job record.

    The log line is written on every event and flushed, so `logs` and previews
    are never behind; the record write is throttled, because a record write
    takes the store lock and the poll interval is what reads it back.
    """

    def __init__(
        self,
        store: JobStore,
        job_id: str,
        throttle_s: float = RECORD_THROTTLE_S,
    ) -> None:
        """Open a Job's progress log for appending.

        Args:
            store: The Job's store.
            job_id: The Job.
            throttle_s: Minimum interval between record writes.
        """
        self._store = store
        self._job_id = job_id
        self._throttle_s = throttle_s
        self.path = precreate(store.log_path(job_id))
        self._handle = self.path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self._phase: str | None = None
        self._last_write = 0.0

    def __call__(self, phase: str, message: str) -> None:
        """Record one progress event.

        Args:
            phase: One of the closed vocabulary in `chinamax.loop.PHASES`,
                stored on the record verbatim.
            message: The event text; control characters are escaped so one
                event is always exactly one log line.
        """
        with self._lock:
            self._write_line(phase, message)
            self._phase = phase
            now = time.monotonic()
            if now - self._last_write >= self._throttle_s:
                self._last_write = now
                self._store.update(
                    self._job_id, {"phase": phase}, expect={STATUS_RUNNING}
                )

    def touch(self) -> None:
        """Refresh ``updatedAt`` (and flush the latest phase) for the heartbeat.

        The phase is carried only once an event has set one: a beat landing
        before the loop's first report would otherwise blank the phase the
        worker's claim wrote, and the heartbeat's job is ``updatedAt``.
        """
        with self._lock:
            self._last_write = time.monotonic()
            changes = {} if self._phase is None else {"phase": self._phase}
            self._store.update(self._job_id, changes, expect={STATUS_RUNNING})

    def close(self) -> None:
        """Close the log handle."""
        with self._lock:
            try:
                self._handle.close()
            except OSError:
                pass

    def _write_line(self, phase: str, message: str) -> None:
        """Append one flushed, single-line event."""
        try:
            self._handle.write(
                f"{utc_now()} [{escape_control(phase)}] {escape_control(message)}\n"
            )
            self._handle.flush()
        except (OSError, ValueError):
            # Observability must never turn valid model work into a failure.
            pass


class Heartbeat:
    """A daemon thread refreshing ``updatedAt`` while the loop blocks.

    The loop blocks for minutes inside one API call or bash command, so the
    heartbeat cannot be driven from it. It refreshes ``updatedAt`` for jobs/02's
    stale detector only — `status --wait` deliberately does not wake on it, or
    every Job would look like it was making progress every 30 s.
    """

    def __init__(
        self, reporter: ProgressReporter, interval_s: float = HEARTBEAT_INTERVAL_S
    ) -> None:
        """Prepare the heartbeat for one Job.

        Args:
            reporter: The Job's reporter, which owns the record write.
            interval_s: How often ``updatedAt`` is refreshed.
        """
        self._reporter = reporter
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._beat, name="chinamax-heartbeat", daemon=True
        )
        self._started = False

    def start(self) -> None:
        """Start beating."""
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        """Stop AND JOIN, so no beat can land after the terminal write."""
        self._stop.set()
        if self._started:
            self._thread.join()

    def _beat(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._reporter.touch()
            except Exception:  # noqa: BLE001 - a heartbeat failure must stay silent
                return


def _stat_start_time(fields: list[str]) -> int | None:
    """Return field 22 from a stat line's post-comm fields."""
    # After the comm come fields 3 onward, so field 22 sits at index 19.
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _identity_holds(target: ProcessInfo, live: ProcessInfo) -> bool:
    """Report whether a live process is still the one that was snapshotted."""
    if target.start_time is None or live.start_time is None:
        return True
    return target.start_time == live.start_time


def _alive_targets(targets: dict[int, ProcessInfo]) -> list[int]:
    """Return the snapshotted pids that are still their original selves."""
    alive = []
    for pid, target in targets.items():
        live = read_process(pid)
        if live is None or live.zombie or not _identity_holds(target, live):
            continue
        alive.append(pid)
    return alive


def _await_exit(targets: dict[int, ProcessInfo], bound_s: float) -> None:
    """Poll until every target is gone, or the bound expires."""
    deadline = time.monotonic() + bound_s
    while _alive_targets(targets) and time.monotonic() < deadline:
        time.sleep(_TERMINATE_POLL_S)


def _rescan(snapshot: dict[int, ProcessInfo]) -> dict[int, ProcessInfo]:
    """Re-walk from the survivors, catching descendants born after the snapshot.

    Only the still-live members can be walked from: a dead parent's children
    have already reparented to init and are no longer reachable by ppid.
    """
    alive = _alive_targets(snapshot)
    if not alive:
        return {}
    table = process_table()
    found: dict[int, ProcessInfo] = {}
    for pid in alive:
        found.update(descendants(pid, table))
    return {
        pid: info
        for pid, info in found.items()
        if pid not in snapshot or _identity_holds(snapshot[pid], info)
    }


def _signal_processes(targets: dict[int, ProcessInfo], number: int) -> None:
    """Signal every distinct process GROUP in a target set.

    Signalling groups rather than pids is what reaches the children the bash
    tool started in their own sessions. A pid whose start time no longer matches
    is skipped rather than signalled, so a recycled pid or pgid is never the
    target, and the caller's OWN group is never signalled.
    """
    own_group = os.getpgid(0)
    groups: set[int] = set()
    for pid, target in targets.items():
        live = read_process(pid)
        if live is None or not _identity_holds(target, live):
            continue
        if live.pgid > 1 and live.pgid != own_group:
            groups.add(live.pgid)
        else:
            _signal_pid(pid, number)
    for group in sorted(groups):
        try:
            os.killpg(group, number)
        except OSError:
            pass


def _signal_pid(pid: int, number: int) -> None:
    """Signal one process, tolerating a race with its exit."""
    try:
        os.kill(pid, number)
    except OSError:
        pass


def _unlink(path: Path) -> None:
    """Remove one file, tolerating absence and never following a symlink."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _unlink_tree(path: Path) -> None:
    """Remove a directory tree, tolerating absence and never following links."""
    if path.is_symlink():
        _unlink(path)
        return
    shutil.rmtree(path, ignore_errors=True)


def _base36(value: int) -> str:
    """Render a non-negative integer in base 36."""
    if value <= 0:
        return "0"
    digits = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(_ID_ALPHABET[remainder])
    return "".join(reversed(digits))


def _absolute_env_dir(name: str) -> Path | None:
    """Return an environment variable as a directory, or None when unusable."""
    value = os.environ.get(name, "").strip()
    if not value or not os.path.isabs(value):
        return None
    return Path(value)


def _git_toplevel(directory: Path) -> Path | None:
    """Return the git toplevel containing a directory, or None."""
    try:
        finished = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = finished.stdout.strip()
    if finished.returncode != 0 or not output:
        return None
    return Path(os.path.realpath(output))


def _write_file(path: Path, text: str) -> None:
    """Publish a file by writing a unique 0600 temporary and renaming over it."""
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(handle, FILE_MODE)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        # Only ever the temporary this call just created.
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
