"""Profile resolution: shipped rows, overlay merge, key lookup, and fail-fast errors."""

from __future__ import annotations

import pytest

from chinamax import profiles
from conftest import OMIT, bash_then_report_script, write_keys, write_overlay

SHIPPED = {
    "deepseek": ("https://api.deepseek.com/anthropic", "deepseek-v4-pro[1m]", "DEEPSEEK_API_KEY"),
    "mimo": ("https://api.xiaomimimo.com/anthropic", "mimo-v2.5-pro[1m]", "MIMO_API_KEY"),
    "glm": ("https://api.z.ai/api/anthropic", "glm-5.2[1m]", "GLM_API_KEY"),
    "minimax": ("https://api.minimax.io/anthropic", "MiniMax-M3[1m]", "MINIMAX_API_KEY"),
    "kimi": ("https://api.moonshot.ai/anthropic", "kimi-k3", "KIMI_API_KEY"),
}


def test_five_shipped_rows():
    """Resolution with no overlay lists exactly the five pro Profiles."""
    resolved = profiles.load_profiles()

    assert set(resolved) == set(SHIPPED)
    for name, expected in SHIPPED.items():
        row = resolved[name]
        assert (row.base_url, row.model, row.api_key_env) == expected
        assert row.max_tokens == profiles.DEFAULT_MAX_TOKENS


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
