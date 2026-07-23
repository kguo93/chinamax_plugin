"""Durable Job state: the per-workspace store, its lock, and the derived index.

Per-Job records under ``jobs/`` are the source of truth; ``state.json`` is a
derived id cache any reader may rebuild. Every mutation goes through one locked
compare-and-swap updater — re-read, check the expected state, publish by
tmp+rename — so a late heartbeat can never resurrect ``running`` over a terminal
write. The lock is a dedicated ``state.lock`` sidecar and never ``state.json``
itself: renaming over a locked file leaves the holder locking an unlinked inode
and silently voids mutual exclusion.

Nothing here deletes or kills a Job (ADR 0004): there is no session-keyed API of
any kind, and no session boundary touches this store.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import re
import string
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from chinamax import ChinamaxError

#: Bumped only for a breaking record change; the schema is additive otherwise —
#: readers ignore unknown fields and default missing optional ones, so an old
#: record stays readable across plugin upgrades (PRD user story 13).
SCHEMA_VERSION = 1

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: A Job in one of these will never move again on its own.
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})

#: Record writes are throttled to one per this interval, matching the
#: `status --wait` poll so a caller cannot outrun the record it polls.
RECORD_THROTTLE_S = 2.0
#: `updatedAt` is refreshed at least this often even when nothing changes. This
#: number and the throttle above move together with jobs/02's stale grace (twice
#: this interval) — drifting one apart breaks stale detection.
HEARTBEAT_INTERVAL_S = 30.0
#: `status --wait` poll interval, on a monotonic clock.
POLL_INTERVAL_S = 2.0
#: The PRD's bounded polling window (~4 min). A larger `--timeout-ms` clamps to
#: it rather than turning a bounded poll into an unbounded block.
WAIT_TIMEOUT_MS = 240_000

#: Records hold prompt text and results, so the store is owner-only throughout.
DIR_MODE = 0o700
FILE_MODE = 0o600

#: The environment variable surface/02's SessionStart hook exports. Recorded for
#: provenance and digest rendering only — no lifecycle behavior keys off it.
SESSION_ID_VARIABLE = "CLAUDE_SESSION_ID"
#: Overrides the interpreter the detached worker runs under. Tests point it at a
#: non-executable path to make a spawn fail for real, with no mocked process layer.
WORKER_PYTHON_VARIABLE = "CHINAMAX_WORKER_PYTHON"

_ID_ALPHABET = string.digits + string.ascii_lowercase
#: The record scan matches this and NOT the bare glob ``jobs/*.json``, which
#: would swallow ``<id>.result.json`` and register phantom Jobs.
_RECORD_RE = re.compile(r"^(task-[0-9a-z]+-[0-9a-z]{6})\.json$")
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


def parse_pid_start_time(stat_text: str) -> int | None:
    """Return field 22 (``starttime``) of a ``/proc/<pid>/stat`` line.

    The split is taken AFTER THE LAST ``)``: field 2 is the parenthesized comm,
    which may itself contain spaces and parentheses. A naive whitespace split
    ships green through every test but the one that pins this, then feeds a
    wrong start-time into jobs/02's kill decision.

    Args:
        stat_text: The raw contents of ``/proc/<pid>/stat``.

    Returns:
        The start time in clock ticks, or None when the line is unusable.
    """
    close = stat_text.rfind(")")
    if close < 0:
        return None
    # After the comm come fields 3 onward, so field 22 sits at index 19.
    fields = stat_text[close + 1 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def read_pid_start_time(pid: int) -> int | None:
    """Read a process's start time, or None where ``/proc`` is unreadable.

    A non-Linux dev machine, or a child that exited before the dispatcher got to
    it, yields None rather than failing the dispatch — which leaves jobs/02 the
    ``kill(pid, 0)`` half of its liveness test.

    Args:
        pid: The process to inspect.

    Returns:
        The start time in clock ticks, or None.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_pid_start_time(text)


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
        originating_session: The Claude session id, or None.

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
            "status": STATUS_QUEUED,
            "createdAt": now,
            "updatedAt": now,
            "logFile": str(log_file),
            "request": request,
        }
    )
    return record


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
        """Resolve an exact id or an unambiguous prefix.

        Args:
            selector: The id or prefix the operator named.

        Returns:
            The matching Job id.

        Raises:
            ChinamaxError: On no match or an ambiguous prefix — including an
                explicit id against an empty store, which is never answered with
                a silent success.
        """
        known = self.job_ids()
        if selector in known:
            return selector
        matches = [job_id for job_id in known if job_id.startswith(selector)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ChinamaxError(
                f"ambiguous Job selector {selector!r}; candidates: {', '.join(matches)}"
            )
        listed = ", ".join(known) if known else "(none)"
        raise ChinamaxError(f"no Job matching {selector!r}; known Jobs: {listed}")

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
