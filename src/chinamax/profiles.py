"""Profile resolution: shipped rows, the user overlay, and API-key lookup.

A Profile is a named provider configuration (base URL, model string, API-key
source). The shipped set ships with the package; `~/.claude/chinamax-profiles.json`
overlays it field by field. There is no default Profile.

Extending to more models: any provider that implements the Anthropic-compatible
Messages API becomes a Profile through the overlay — a row with `name`,
`base_url` (the provider's `/anthropic` endpoint), `model`, and `api_key_env`,
optionally `request_extras` (a dict of extra Messages-request kwargs, e.g. an
always-on reasoning knob, merged verbatim into every request) — plus the
matching key line in `~/.claude/model-keys.env`. The setup doctor
scaffolds that key file from the RESOLVED rows (`doctor.key_template_text`
iterates `load_profiles()`), so a new overlay Profile's key line appears in the
next scaffold with no code change here.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from chinamax import ChinamaxError

DEFAULT_MAX_TOKENS = 32000
ROW_FIELDS = ("name", "base_url", "model", "api_key_env", "max_tokens", "request_extras")
REQUIRED_ROW_FIELDS = ("base_url", "model", "api_key_env")
#: Request keys a Profile's ``request_extras`` may never carry — checked at the
#: top level and one level inside an ``extra_body`` value. The first five are the
#: Runtime-built request fields; the last four are client/transport-policy kwargs:
#: ``timeout`` would override the watchdog-backstop client timeout, ``extra_headers``
#: the Profile's bearer auth, ``extra_query`` its query policy, and ``stream`` the
#: streaming contract (decision 3 records the per-key mechanism).
RESERVED_REQUEST_KEYS = (
    "model",
    "max_tokens",
    "system",
    "tools",
    "messages",
    "stream",
    "timeout",
    "extra_headers",
    "extra_query",
)
OVERLAY_FILENAME = "chinamax-profiles.json"
KEYS_FILENAME = "model-keys.env"


@dataclass(frozen=True)
class Profile:
    """One provider configuration a Job can run against."""

    name: str
    base_url: str
    model: str
    api_key_env: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    request_extras: dict = field(default_factory=dict)


def overlay_path() -> Path:
    """Return the optional user overlay path (resolved through ``Path.home()``)."""
    return Path.home() / ".claude" / OVERLAY_FILENAME


def keys_path() -> Path:
    """Return the API-key file path (resolved through ``Path.home()``)."""
    return Path.home() / ".claude" / KEYS_FILENAME


def load_profiles() -> dict[str, Profile]:
    """Resolve the shipped Profiles merged field-by-field with the user overlay.

    Returns:
        Profiles keyed by name.

    Raises:
        ChinamaxError: If the overlay file is malformed or an overlay row is invalid.
    """
    resolved = _load_shipped()
    path = overlay_path()
    if not path.exists():
        return resolved
    for row in _read_overlay(path):
        name = row["name"]
        fields = {key: value for key, value in row.items() if key != "name"}
        if name in resolved:
            resolved[name] = replace(resolved[name], **fields)
            continue
        missing = [field for field in REQUIRED_ROW_FIELDS if field not in fields]
        if missing:
            raise ChinamaxError(
                f"{path}: new profile row {name!r} must define {', '.join(missing)}"
            )
        resolved[name] = Profile(name=name, **fields)
    return resolved


def resolve_profile(name: str) -> Profile:
    """Look up one Profile by name.

    Args:
        name: The Profile name a job spec asked for.

    Returns:
        The resolved Profile.

    Raises:
        ChinamaxError: If no Profile carries that name.
    """
    resolved = load_profiles()
    if name not in resolved:
        raise ChinamaxError(
            f"unknown profile {name!r}; configured profiles: {format_available(resolved)}"
        )
    return resolved[name]


def format_available(resolved: dict[str, Profile] | None = None) -> str:
    """Return the configured Profile names as a comma-separated string."""
    if resolved is None:
        resolved = load_profiles()
    return ", ".join(sorted(resolved))


def load_keys() -> dict[str, str]:
    """Parse ``~/.claude/model-keys.env`` into an env-var name → value mapping.

    Blank lines and ``#`` comment lines are ignored. Values are unquoted by shell
    rules, because the real file single-quotes some of its values and is consumed
    elsewhere by bash-sourcing it.

    Returns:
        The parsed variables; empty when the file does not exist.
    """
    path = keys_path()
    if not path.exists():
        return {}
    keys: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, raw = stripped.partition("=")
        if not separator:
            continue
        try:
            fields = shlex.split(raw)
        except ValueError:
            fields = []
        keys[name.strip()] = fields[0] if fields else ""
    return keys


def resolve_key(profile: Profile) -> str:
    """Return the API key a Profile authenticates with.

    Args:
        profile: The resolved Profile.

    Returns:
        The key value read from the key file.

    Raises:
        ChinamaxError: If the Profile's env-var name is absent or empty there.
    """
    value = load_keys().get(profile.api_key_env, "")
    if not value:
        raise ChinamaxError(
            f"missing API key: {profile.api_key_env} is not set in {keys_path()}"
        )
    return value


def _load_shipped() -> dict[str, Profile]:
    """Read the Profile rows shipped inside the package."""
    raw = (
        resources.files("chinamax")
        .joinpath("data", "profiles.json")
        .read_text(encoding="utf-8")
    )
    return {row["name"]: Profile(**row) for row in json.loads(raw)}


def _read_overlay(path: Path) -> list[dict]:
    """Read and validate the user overlay file.

    Args:
        path: The overlay file path.

    Returns:
        The validated overlay rows in file order.

    Raises:
        ChinamaxError: On malformed JSON, a bad row shape, a duplicate name, an
            unknown field, a non-positive/non-integer ``max_tokens``, or a
            ``request_extras`` that is not a JSON object or carries a reserved
            request key (at the top level or inside an ``extra_body`` value).
    """
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ChinamaxError(f"{path}: malformed JSON ({exc})") from exc
    if not isinstance(rows, list):
        raise ChinamaxError(f"{path}: expected a JSON array of profile rows")

    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ChinamaxError(f"{path}: row {index} is not a JSON object")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            raise ChinamaxError(f"{path}: row {index} needs a non-empty 'name'")
        if name in seen:
            raise ChinamaxError(f"{path}: duplicate profile row {name!r}")
        seen.add(name)
        unknown = sorted(set(row) - set(ROW_FIELDS))
        if unknown:
            raise ChinamaxError(
                f"{path}: row {name!r} has unknown field(s): {', '.join(unknown)}"
            )
        for field in REQUIRED_ROW_FIELDS:
            if field in row and (not isinstance(row[field], str) or not row[field]):
                raise ChinamaxError(
                    f"{path}: row {name!r} field {field!r} must be a non-empty string"
                )
        if "max_tokens" in row:
            max_tokens = row["max_tokens"]
            if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
                raise ChinamaxError(
                    f"{path}: row {name!r} field 'max_tokens' must be a positive integer"
                )
        if "request_extras" in row:
            extras = row["request_extras"]
            if not isinstance(extras, dict):
                raise ChinamaxError(
                    f"{path}: row {name!r} field 'request_extras' must be a JSON object"
                )
            # The SDK merges an ``extra_body`` value into the JSON body ROOT with
            # precedence, so a reserved key nested there overrides the Runtime on
            # the wire exactly like a top-level one — check both levels.
            bad = set(extras) & set(RESERVED_REQUEST_KEYS)
            if isinstance(extras.get("extra_body"), dict):
                bad |= set(extras["extra_body"]) & set(RESERVED_REQUEST_KEYS)
            if bad:
                raise ChinamaxError(
                    f"{path}: row {name!r} 'request_extras' may not set reserved "
                    f"key(s): {', '.join(sorted(bad))}"
                )
    return rows
