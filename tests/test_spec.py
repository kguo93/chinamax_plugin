"""Job-spec validation fails fast, before any provider call."""

from __future__ import annotations

import pytest

from chinamax.spec import parse_spec
from conftest import OMIT, bash_then_report_script

CASES = {
    "missing_required_field": ({"prompt": OMIT}, "prompt"),
    "wrong_typed_field": ({"prompt": 17}, "prompt"),
    "unknown_key": ({"retries": 3}, "retries"),
    "missing_workspace": (None, "workspace"),
    # A supervision override that got through would give the Job an
    # instantly-tripping watchdog, or a ladder that never calls the provider.
    "non_positive_inactivity": ({"inactivity_timeout_s": 0}, "inactivity_timeout_s"),
    "empty_ladder": ({"ladder_attempts": 0}, "ladder_attempts"),
    "boolean_ladder": ({"ladder_attempts": True}, "ladder_attempts"),
    # An explicit model must be a non-empty string; the provider judges validity.
    "empty_model": ({"model": ""}, "model"),
    "wrong_typed_model": ({"model": 3}, "model"),
    # host must be a known Host enum value; mcp must be an array of names.
    "bad_host": ({"host": "bogus"}, "host"),
    "wrong_typed_host": ({"host": 5}, "host"),
    "wrong_typed_mcp": ({"mcp": "server"}, "mcp"),
    "bad_mcp_element": ({"mcp": [1]}, "mcp"),
    "empty_mcp_element": ({"mcp": [""]}, "mcp"),
    # The pinned Policy toggles must be booleans (a non-bool would silently ride
    # the record and mis-govern the worker).
    "wrong_typed_memory_enabled": ({"memory_enabled": "yes"}, "memory_enabled"),
    "wrong_typed_hooks_enabled": ({"hooks_enabled": 1}, "hooks_enabled"),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_spec_validation(job_env, capsys, case):
    """Each invalid spec exits non-zero naming its field, with no request sent."""
    env = job_env(bash_then_report_script())
    overrides, field = CASES[case]
    if overrides is None:
        overrides = {"workspace": str(env.workspace / "does-not-exist")}

    assert env.run(env.spec(**overrides)) != 0

    assert field in capsys.readouterr().err
    assert env.requests == []


def test_model_field_optional_and_carried(job_env):
    """An explicit model rides the spec; absent leaves it None; it is a known field."""
    env = job_env(bash_then_report_script())

    assert parse_spec(env.spec()).model is None
    assert parse_spec(env.spec(model="custom-m")).model == "custom-m"


def test_host_and_mcp_fields_optional_and_carried(job_env):
    """host/mcp are optional public spec fields, carried when present."""
    env = job_env(bash_then_report_script())

    default = parse_spec(env.spec())
    assert default.host is None
    assert default.mcp is None

    carried = parse_spec(env.spec(host="codex", mcp=["a", "b"]))
    assert carried.host == "codex"
    assert carried.mcp == ["a", "b"]
    # An explicit empty list is meaningful (no servers) and is preserved.
    assert parse_spec(env.spec(mcp=[])).mcp == []


def test_policy_toggle_fields_optional_and_defaulted(job_env):
    """memory_enabled/hooks_enabled default False when absent and carry when set."""
    env = job_env(bash_then_report_script())

    default = parse_spec(env.spec())
    assert default.memory_enabled is False
    assert default.hooks_enabled is False

    carried = parse_spec(env.spec(memory_enabled=True, hooks_enabled=False))
    assert carried.memory_enabled is True
    assert carried.hooks_enabled is False
