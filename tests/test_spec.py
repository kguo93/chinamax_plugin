"""Job-spec validation fails fast, before any provider call."""

from __future__ import annotations

import pytest

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
