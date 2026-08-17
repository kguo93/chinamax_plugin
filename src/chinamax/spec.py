"""Job-spec parsing and validation — the Runtime's public dispatch contract.

The spec names the workspace, the Profile, the prompt, and the two paths the
Runtime writes (the Thread transcript and the verbatim result). The caller owns
those paths, because the durable state layout belongs to the jobs scope.

``seed_transcript`` is how a Thread survives: with it set, a non-empty
transcript is read into the loop's history and appended to, rather than
truncated by the fresh-run default. Resume and worker relaunch both stand on it.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from chinamax import ChinamaxError, profiles
from chinamax.host import Host

REQUIRED_FIELDS = ("workspace", "profile", "prompt", "transcript_path", "result_path")
OPTIONAL_FIELDS = (
    "write",
    "model",
    "job_id",
    "seed_transcript",
    "bash_timeout_s",
    "inactivity_timeout_s",
    "ladder_attempts",
    "backoff_base_s",
    "backoff_cap_s",
    "host",
    "mcp",
    "memory_enabled",
    "hooks_enabled",
)
_ABSOLUTE_PATH_FIELDS = ("workspace", "transcript_path", "result_path")

#: Per-command bash timeout when the spec does not override it (ADR 0002's ten
#: minutes). It bounds one command, never the Job: expiry is an observation.
DEFAULT_BASH_TIMEOUT_S = 600.0

#: The supervision knobs a dispatch may override. None means "not overridden":
#: `liveness.build_config` leaves the Runtime's default in place.
_SUPERVISION_NUMBERS = ("inactivity_timeout_s", "backoff_base_s", "backoff_cap_s")


@dataclass(frozen=True)
class JobSpec:
    """One validated dispatch."""

    workspace: Path
    profile: str
    prompt: str
    transcript_path: Path
    result_path: Path
    write: bool = True
    model: str | None = None
    job_id: str | None = None
    seed_transcript: bool = False
    bash_timeout_s: float = DEFAULT_BASH_TIMEOUT_S
    inactivity_timeout_s: float | None = None
    ladder_attempts: int | None = None
    backoff_base_s: float | None = None
    backoff_cap_s: float | None = None
    #: The Job's Host (``claude``/``codex``), keying its Policy hook, Memory and
    #: MCP discovery. Optional in the public spec format: direct ``exec`` specs
    #: omit it, and ``run_exec`` injects the process-bound Host's value.
    host: str | None = None
    #: The RESOLVED Worker-MCP server-name selection pinned to the Thread: None
    #: (all discovered), ``[]`` (none), or an explicit name list.
    mcp: list[str] | None = None
    #: The pinned Policy toggles governing Memory injection and Policy hooks
    #: (ADR 0016, amended 0.7.0). Default OFF: an absent pin resolves to False.
    memory_enabled: bool = False
    hooks_enabled: bool = False


def load_spec(path: str | Path) -> JobSpec:
    """Read and validate a job spec from a JSON file.

    Args:
        path: Path to the job-spec JSON file.

    Returns:
        The validated spec.

    Raises:
        ChinamaxError: If the file is unreadable, is not JSON, or fails validation.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChinamaxError(f"cannot read job spec {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChinamaxError(f"{path}: malformed JSON ({exc})") from exc
    return parse_spec(data)


def parse_spec(data: object) -> JobSpec:
    """Validate a decoded job spec.

    Args:
        data: The decoded JSON document.

    Returns:
        The validated spec.

    Raises:
        ChinamaxError: Naming the offending field, before any provider call.
    """
    if not isinstance(data, dict):
        raise ChinamaxError("job spec must be a JSON object")

    unknown = sorted(set(data) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS))
    if unknown:
        raise ChinamaxError(f"job spec has unknown field(s): {', '.join(unknown)}")

    if not data.get("profile"):
        raise ChinamaxError(
            "job spec field 'profile' is required (there is no default Profile); "
            f"configured profiles: {profiles.format_available()}"
        )
    for field in REQUIRED_FIELDS:
        if field not in data:
            raise ChinamaxError(f"job spec field {field!r} is required")
    for field in REQUIRED_FIELDS:
        if not isinstance(data[field], str) or not data[field]:
            raise ChinamaxError(f"job spec field {field!r} must be a non-empty string")
    for field in _ABSOLUTE_PATH_FIELDS:
        if not os.path.isabs(data[field]):
            raise ChinamaxError(f"job spec field {field!r} must be an absolute path")

    workspace = Path(data["workspace"])
    if not workspace.is_dir():
        raise ChinamaxError(
            f"job spec field 'workspace' must name an existing directory: {workspace}"
        )

    transcript_path = Path(os.path.normpath(data["transcript_path"]))
    result_path = Path(os.path.normpath(data["result_path"]))
    if transcript_path == result_path:
        raise ChinamaxError(
            "job spec fields 'transcript_path' and 'result_path' must name "
            f"different files: {transcript_path}"
        )

    write = data.get("write", True)
    if not isinstance(write, bool):
        raise ChinamaxError("job spec field 'write' must be a boolean")
    seed_transcript = data.get("seed_transcript", False)
    if not isinstance(seed_transcript, bool):
        raise ChinamaxError("job spec field 'seed_transcript' must be a boolean")
    memory_enabled = data.get("memory_enabled", False)
    if not isinstance(memory_enabled, bool):
        raise ChinamaxError("job spec field 'memory_enabled' must be a boolean")
    hooks_enabled = data.get("hooks_enabled", False)
    if not isinstance(hooks_enabled, bool):
        raise ChinamaxError("job spec field 'hooks_enabled' must be a boolean")
    job_id = data.get("job_id")
    if job_id is not None and not isinstance(job_id, str):
        raise ChinamaxError("job spec field 'job_id' must be a string")
    model = data.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ChinamaxError("job spec field 'model' must be a non-empty string")

    host = data.get("host")
    if host is not None:
        if not isinstance(host, str):
            raise ChinamaxError("job spec field 'host' must be a string")
        try:
            Host(host)
        except ValueError as exc:
            raise ChinamaxError(
                "job spec field 'host' must be 'claude' or 'codex', not "
                f"{host!r}"
            ) from exc
    mcp = data.get("mcp")
    if mcp is not None and (
        not isinstance(mcp, list) or not all(isinstance(name, str) and name for name in mcp)
    ):
        raise ChinamaxError(
            "job spec field 'mcp' must be an array of non-empty server-name strings"
        )

    bash_timeout_s = data.get("bash_timeout_s")
    return JobSpec(
        workspace=workspace,
        profile=data["profile"],
        prompt=data["prompt"],
        transcript_path=transcript_path,
        result_path=result_path,
        write=write,
        model=model,
        job_id=job_id,
        seed_transcript=seed_transcript,
        host=host,
        mcp=list(mcp) if mcp is not None else None,
        memory_enabled=memory_enabled,
        hooks_enabled=hooks_enabled,
        bash_timeout_s=(
            DEFAULT_BASH_TIMEOUT_S
            if bash_timeout_s is None
            else _positive_number(bash_timeout_s, "bash_timeout_s")
        ),
        ladder_attempts=_optional_ladder_attempts(data.get("ladder_attempts")),
        **{
            field: _optional_positive_number(data.get(field), field)
            for field in _SUPERVISION_NUMBERS
        },
    )


def _positive_number(value: object, field: str) -> float:
    """Validate one finite, strictly positive numeric field.

    A bad value here would kill every command the Job runs, or leave it with an
    instantly-tripping watchdog, so it fails spec validation rather than
    degrading silently. ``bool`` is rejected explicitly: it subclasses ``int``,
    so a bare ``true`` would otherwise sail through an
    ``isinstance(value, (int, float))`` check as a one-second timeout.

    Args:
        value: The supplied value.
        field: The job-spec field name, for the error message.

    Returns:
        The value as a float.

    Raises:
        ChinamaxError: If the value is boolean, non-numeric, non-finite, or not
            strictly positive.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ChinamaxError(
            f"job spec field {field!r} must be a finite positive number of "
            f"seconds, not {value!r}"
        )
    return float(value)


def _optional_positive_number(value: object, field: str) -> float | None:
    """Validate an optional supervision number, or pass None through."""
    return None if value is None else _positive_number(value, field)


def _optional_ladder_attempts(value: object) -> int | None:
    """Validate the optional retry-ladder size.

    A ladder of zero attempts would never call the provider at all, so a
    violation fails validation here rather than producing a Job that fails
    without ever having tried. ``bool`` is rejected for the same reason it is
    above.

    Args:
        value: The spec's ``ladder_attempts``, or None when it was omitted.

    Returns:
        The ladder size, or None when the Runtime's default stands.

    Raises:
        ChinamaxError: If the value is boolean, non-integer, or below 1.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChinamaxError(
            "job spec field 'ladder_attempts' must be an integer of at least 1, "
            f"not {value!r}"
        )
    return value
