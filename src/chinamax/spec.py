"""Job-spec parsing and validation — the Runtime's public dispatch contract.

The spec names the workspace, the Profile, the prompt, and the two paths the
Runtime writes (the Thread transcript and the verbatim result). The caller owns
those paths, because the durable state layout belongs to the jobs scope.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from chinamax import ChinamaxError, profiles

REQUIRED_FIELDS = ("workspace", "profile", "prompt", "transcript_path", "result_path")
OPTIONAL_FIELDS = ("write", "job_id")
_ABSOLUTE_PATH_FIELDS = ("workspace", "transcript_path", "result_path")


@dataclass(frozen=True)
class JobSpec:
    """One validated dispatch."""

    workspace: Path
    profile: str
    prompt: str
    transcript_path: Path
    result_path: Path
    write: bool = True
    job_id: str | None = None


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
    job_id = data.get("job_id")
    if job_id is not None and not isinstance(job_id, str):
        raise ChinamaxError("job spec field 'job_id' must be a string")

    return JobSpec(
        workspace=workspace,
        profile=data["profile"],
        prompt=data["prompt"],
        transcript_path=transcript_path,
        result_path=result_path,
        write=write,
        job_id=job_id,
    )
