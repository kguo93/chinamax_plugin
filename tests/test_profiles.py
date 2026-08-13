"""Profile resolution: shipped rows, overlay merge, key lookup, and fail-fast errors."""

from __future__ import annotations

import pytest

from chinamax import ChinamaxError, profiles
from conftest import OMIT, bash_then_report_script, write_keys, write_overlay

SHIPPED = {
    "deepseek": ("https://api.deepseek.com/anthropic", "deepseek-v4-pro[1m]", "DEEPSEEK_API_KEY"),
    "mimo": ("https://api.xiaomimimo.com/anthropic", "mimo-v2.5-pro", "MIMO_API_KEY"),
    "glm": ("https://api.z.ai/api/anthropic", "glm-5.2", "GLM_API_KEY"),
    "minimax": ("https://api.minimax.io/anthropic", "MiniMax-M3[1m]", "MINIMAX_API_KEY"),
    "kimi": ("https://api.moonshot.ai/anthropic", "kimi-k3", "KIMI_API_KEY"),
    "qwen": ("https://dashscope-intl.aliyuncs.com/apps/anthropic", "qwen3.8-max", "QWEN_API_KEY"),
}

#: The exact always-on reasoning knob each shipped row ships (ground truth,
#: live-verified 2026-08-01); every shipped Profile carries one.
SHIPPED_EXTRAS = {
    "deepseek": {"extra_body": {"reasoning": {"effort": "max"}}},
    "mimo": {"extra_body": {"reasoning_effort": "high"}},
    "glm": {"thinking": {"type": "enabled"}},
    "minimax": {"thinking": {"type": "adaptive"}},
    "kimi": {"extra_body": {"reasoning_effort": "max"}},
    "qwen": {"thinking": {"type": "enabled"}},
}


def test_six_shipped_rows():
    """Resolution with no overlay lists exactly the six pro Profiles."""
    resolved = profiles.load_profiles()

    assert set(resolved) == set(SHIPPED)
    reserved = set(profiles.RESERVED_REQUEST_KEYS)
    for name, expected in SHIPPED.items():
        row = resolved[name]
        assert (row.base_url, row.model, row.api_key_env) == expected
        assert row.max_tokens == profiles.DEFAULT_MAX_TOKENS
        assert row.request_extras == SHIPPED_EXTRAS[name]
        # Shipped rows bypass _read_overlay (trusted package data), so THIS test
        # is the reserved-key guard: no shipped row's extras collide with a
        # reserved request key at the top level OR inside an extra_body value.
        assert not set(row.request_extras) & reserved
        extra_body = row.request_extras.get("extra_body")
        if isinstance(extra_body, dict):
            assert not set(extra_body) & reserved


def test_override_merge(keyless_home):
    """An overlay row wins for its Profile and leaves the other four untouched."""
    write_overlay(keyless_home, [{"name": "deepseek", "model": "deepseek-override"}])

    resolved = profiles.load_profiles()

    assert resolved["deepseek"].model == "deepseek-override"
    for name in set(SHIPPED) - {"deepseek"}:
        assert (
            resolved[name].base_url,
            resolved[name].model,
            resolved[name].api_key_env,
        ) == SHIPPED[name]


def test_override_adds_and_partially_merges_row(keyless_home):
    """A partial row merges field by field; an unknown name adds a Profile."""
    write_overlay(
        keyless_home,
        [
            {"name": "deepseek", "model": "deepseek-override"},
            {
                "name": "local",
                "base_url": "http://127.0.0.1:9/anthropic",
                "model": "local-1",
                "api_key_env": "LOCAL_API_KEY",
            },
        ],
    )

    resolved = profiles.load_profiles()

    assert resolved["deepseek"].model == "deepseek-override"
    assert resolved["deepseek"].base_url == SHIPPED["deepseek"][0]
    assert resolved["deepseek"].api_key_env == SHIPPED["deepseek"][2]
    assert set(resolved) == set(SHIPPED) | {"local"}
    assert resolved["local"].base_url == "http://127.0.0.1:9/anthropic"
    assert resolved["local"].max_tokens == profiles.DEFAULT_MAX_TOKENS


def test_overlay_replaces_request_extras_wholesale(keyless_home):
    """An overlay row's request_extras REPLACES the shipped dict — never merges."""
    write_overlay(
        keyless_home,
        [{"name": "deepseek", "request_extras": {"thinking": {"type": "enabled"}}}],
    )

    resolved = profiles.load_profiles()

    assert resolved["deepseek"].request_extras == {"thinking": {"type": "enabled"}}
    # The shipped extra_body nesting is GONE, not merged under the new dict.
    assert "extra_body" not in resolved["deepseek"].request_extras


def test_overlay_empty_request_extras_disables_reasoning(keyless_home):
    """An explicit empty object turns a shipped Profile's reasoning off."""
    write_overlay(keyless_home, [{"name": "deepseek", "request_extras": {}}])

    assert profiles.load_profiles()["deepseek"].request_extras == {}


def test_overlay_non_object_request_extras_rejected(keyless_home):
    """A non-object request_extras fails overlay load, naming the row."""
    write_overlay(keyless_home, [{"name": "deepseek", "request_extras": "max"}])

    with pytest.raises(ChinamaxError, match="deepseek.*must be a JSON object"):
        profiles.load_profiles()


def test_overlay_reserved_request_extras_key_rejected(keyless_home):
    """A top-level reserved key in request_extras fails overlay load, naming it."""
    write_overlay(keyless_home, [{"name": "deepseek", "request_extras": {"max_tokens": 64}}])

    with pytest.raises(ChinamaxError, match="reserved key.*max_tokens"):
        profiles.load_profiles()


def test_overlay_nested_reserved_extra_body_key_rejected(keyless_home):
    """A reserved key nested inside extra_body is rejected too (the wire backdoor)."""
    write_overlay(
        keyless_home,
        [{"name": "deepseek", "request_extras": {"extra_body": {"max_tokens": 64}}}],
    )

    with pytest.raises(ChinamaxError, match="reserved key.*max_tokens"):
        profiles.load_profiles()


def test_overlay_reserved_client_policy_key_rejected(keyless_home):
    """A client/transport-policy key (timeout) is reserved, naming it on rejection."""
    write_overlay(keyless_home, [{"name": "deepseek", "request_extras": {"timeout": 5}}])

    with pytest.raises(ChinamaxError, match="reserved key.*timeout"):
        profiles.load_profiles()


def test_overlay_reserved_credential_channel_key_rejected(keyless_home):
    """extra_headers is reserved so extras can never carry credentials."""
    write_overlay(
        keyless_home,
        [{"name": "deepseek", "request_extras": {"extra_headers": {"authorization": "x"}}}],
    )

    with pytest.raises(ChinamaxError, match="reserved key.*extra_headers"):
        profiles.load_profiles()


def test_overlay_new_profile_carries_request_extras(keyless_home):
    """A brand-new overlay Profile may carry request_extras (the field is optional)."""
    write_overlay(
        keyless_home,
        [
            {
                "name": "local",
                "base_url": "http://127.0.0.1:9/anthropic",
                "model": "local-1",
                "api_key_env": "LOCAL_API_KEY",
                "request_extras": {"thinking": {"type": "enabled"}},
            }
        ],
    )

    resolved = profiles.load_profiles()

    assert resolved["local"].request_extras == {"thinking": {"type": "enabled"}}


def test_keys_resolved_from_env_file(keyless_home):
    """Keys come from model-keys.env, unquoted by shell rules."""
    (keyless_home / ".claude" / "model-keys.env").write_text(
        "# worker model keys\n"
        "DEEPSEEK_API_KEY='sk-single-quoted'\n"
        "\n"
        "MIMO_API_KEY=sk-bare\n"
        'KIMI_API_KEY="sk-double-quoted"\n',
        encoding="utf-8",
    )

    assert profiles.resolve_key(profiles.resolve_profile("deepseek")) == "sk-single-quoted"
    assert profiles.resolve_key(profiles.resolve_profile("mimo")) == "sk-bare"
    assert profiles.resolve_key(profiles.resolve_profile("kimi")) == "sk-double-quoted"


def test_missing_profile_fails_fast(job_env, capsys):
    """A dispatch naming no Profile exits non-zero and lists the valid Profiles."""
    env = job_env(bash_then_report_script())

    assert env.run(env.spec(profile=OMIT)) != 0

    stderr = capsys.readouterr().err
    assert "profile" in stderr
    assert all(name in stderr for name in SHIPPED)
    assert env.requests == []


def test_unknown_profile_fails_fast(job_env, capsys):
    """An unknown Profile exits non-zero and lists the valid Profiles."""
    env = job_env(bash_then_report_script())

    assert env.run(env.spec(profile="no-such-profile")) != 0

    stderr = capsys.readouterr().err
    assert "no-such-profile" in stderr
    assert all(name in stderr for name in SHIPPED)
    assert env.requests == []


def test_missing_key_fails_fast(job_env, keyless_home, capsys):
    """A Profile whose key is absent from model-keys.env exits non-zero, naming it."""
    env = job_env(bash_then_report_script())
    write_keys(keyless_home, {"MIMO_API_KEY": "sk-fake-mimo"})

    assert env.run() != 0

    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err
    assert env.requests == []
