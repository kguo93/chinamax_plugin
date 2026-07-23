"""Provider client construction.

The Profile is the only source of endpoint and credential, so the ambient
``ANTHROPIC_*`` variables are removed from the process environment before any
client exists: the SDK falls back to ``ANTHROPIC_API_KEY`` whenever ``api_key``
is None, and its ``x-api-key`` header wins over bearer auth — which would send
an operator's unrelated Anthropic key to a worker provider.
"""

from __future__ import annotations

import os

import anthropic
import httpx

from chinamax.liveness import DEFAULT_INACTIVITY_TIMEOUT_S
from chinamax.profiles import Profile

AMBIENT_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

#: Deliberately small, and the reason the timeout is structured rather than
#: scalar: a blackholed IP must fail connect promptly instead of inheriting the
#: read budget.
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 60.0
POOL_TIMEOUT_S = 60.0
#: The read timeout sits this far behind the inactivity watchdog, which always
#: fires first. It is a backstop, not the supervision mechanism.
READ_TIMEOUT_MARGIN_S = 60.0


def sanitize_environment() -> None:
    """Remove the ambient ``ANTHROPIC_*`` variables from ``os.environ``."""
    for name in AMBIENT_VARIABLES:
        os.environ.pop(name, None)


def build_client(
    profile: Profile,
    api_key: str,
    inactivity_timeout_s: float = DEFAULT_INACTIVITY_TIMEOUT_S,
) -> anthropic.Anthropic:
    """Build the SDK client for one Profile.

    Bearer auth matches how these endpoints are already driven in production.
    ``max_retries=0`` is deliberate: left at its defaults the SDK retries
    408/409/429/5xx twice, which would silently turn the Runtime's six ladder
    attempts into up to eighteen unlogged requests and pre-empt the inactivity
    bound. The ladder is the only retry layer.

    Args:
        profile: The resolved Profile.
        api_key: The Profile's API key.
        inactivity_timeout_s: The loop's inactivity bound, which the read
            timeout backstops.

    Returns:
        A client pointed at the Profile's base URL.
    """
    return anthropic.Anthropic(
        base_url=profile.base_url,
        auth_token=api_key,
        max_retries=0,
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=inactivity_timeout_s + READ_TIMEOUT_MARGIN_S,
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        ),
    )
