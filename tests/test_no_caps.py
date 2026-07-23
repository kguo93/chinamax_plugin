"""No wall-clock limit and no turn limit exist anywhere in the loop (ADR 0002)."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime

from chinamax.liveness import LoopConfig
from conftest import REPORT_PAYLOAD, Sleeper, SteppingClock, loop_config, report_turn, text_block, turn

#: Comfortably past the "100+ turns" the acceptance criterion asks for.
TURNS = 120
#: Every read of the injected clock jumps this far, so the run's simulated wall
#: clock passes four hours between consecutive turns.
STEP_S = 4.5 * 3600
FOUR_HOURS_S = 4 * 3600

#: `LoopConfig`'s entire surface. A cap added under ANY name — max_duration,
#: loop_limit, deadline — fails this rather than slipping past a name pattern.
EXPECTED_CONFIG_FIELDS = {
    "inactivity_timeout_s",
    "ladder_attempts",
    "backoff_base_s",
    "backoff_cap_s",
    "clock",
    "sleeper",
    "jitter",
}


def test_hundred_turn_run_completes(job_env):
    """A 120-turn run under a simulated four-hour-per-turn clock still completes.

    The 120 turns are the real proof: the field allowlist below guards the
    configuration surface only, and a cap hidden as a module constant would slip
    past it.
    """
    script = [turn([text_block(f"thinking, step {index}")]) for index in range(TURNS)]
    env = job_env(script + [report_turn()])
    config = loop_config(Sleeper(), clock=SteppingClock(step=STEP_S))

    assert env.run(config=config) == 0

    assert env.result() == REPORT_PAYLOAD
    assert len(env.requests) == TURNS + 1

    stamps = [
        json.loads(line)["ts"]
        for line in env.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert _seconds_between(stamps[0], stamps[-1]) > FOUR_HOURS_S

    assert {item.name for item in fields(LoopConfig)} == EXPECTED_CONFIG_FIELDS


def _seconds_between(first: str, last: str) -> float:
    """Return the elapsed seconds between two ISO-8601 transcript timestamps."""
    return (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds()
