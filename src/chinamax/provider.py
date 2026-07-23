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

from chinamax.profiles import Profile

AMBIENT_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def sanitize_environment() -> None:
    """Remove the ambient ``ANTHROPIC_*`` variables from ``os.environ``."""
    for name in AMBIENT_VARIABLES:
        os.environ.pop(name, None)


def build_client(profile: Profile, api_key: str) -> anthropic.Anthropic:
    """Build the SDK client for one Profile.

    Bearer auth matches how these endpoints are already driven in production.
    ``max_retries=0`` is deliberate: the SDK's own retries would otherwise nest
    underneath the Runtime's retry ladder and make its accounting wrong.

    Args:
        profile: The resolved Profile.
        api_key: The Profile's API key.

    Returns:
        A client pointed at the Profile's base URL.
    """
    return anthropic.Anthropic(
        base_url=profile.base_url,
        auth_token=api_key,
        max_retries=0,
    )
