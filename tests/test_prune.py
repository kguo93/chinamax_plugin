"""Pruning: the oldest finished Jobs go with every artifact, the live ones stay."""

from __future__ import annotations

import json

import pytest

from chinamax import ChinamaxError, state
from conftest import (
    aged,
    bash_then_report_script,
    build_record,
    dead_pid,
    job_artifacts,
    job_leftovers,
    wait_for,
    write_artifacts,
)

#: More than the cap, so the oldest have to go.
SEEDED = 55


def seed(store, workspace, **fields) -> str:
    """Publish one Job record with every artifact of the state layout beside it."""
    job_id = build_record(store, workspace=workspace, **fields)
    write_artifacts(store, job_id)
    return job_id


def seed_finished(store, workspace) -> list[str]:
    """Seed `SEEDED` finished Jobs, oldest first by ``completedAt``."""
    return [
        seed(
            store,
            workspace,
            status=state.STATUS_COMPLETED,
            completed_at=aged(SEEDED - index),
        )
        for index in range(SEEDED)
    ]


def seed_running(store, workspace) -> str:
    """Seed a Job whose worker is alive (no recorded pid is never probed)."""
    return seed(store, workspace, status=state.STATUS_RUNNING)


def seed_interrupted(store, workspace) -> str:
    """Seed a Job whose worker is provably gone and whose heartbeat is stale."""
    return seed(
        store,
        workspace,
        status=state.STATUS_RUNNING,
        pid=dead_pid(),
        updated_at=aged(state.STALE_GRACE_S * 2),
    )


def finished_ids(store) -> list[str]:
    """Return every id whose STORED status is finished."""
    records, _ = store.load_records()
    return sorted(
        record["id"] for record in records if record["status"] in state.FINISHED_STATUSES
    )


def test_prunes_beyond_cap_files_removed(dispatch_env):
    """A dispatch prunes the oldest finished Jobs, artifacts and index alike."""
    env = dispatch_env(bash_then_report_script())
    store = env.store.ensure()
    seeded = seed_finished(store, env.workspace)
    running = seed_running(store, env.workspace)
    interrupted = seed_interrupted(store, env.workspace)

    code, fresh = env.dispatch()
    assert code == 0

    # The dispatch prunes, and so does the worker's terminal transition — which
    # adds a 56th finished Job, so exactly six of the seeded ones go. Waiting on
    # the count alone would pass at the FIRST prune, before the Job finished.
    assert wait_for(
        lambda: fresh in finished_ids(store)
        and len(finished_ids(store)) == state.PRUNE_KEEP,
        90.0,
    ), f"settled at {len(finished_ids(store))} finished Jobs"
    dropped = seeded[: SEEDED + 1 - state.PRUNE_KEEP]
    kept = seeded[SEEDED + 1 - state.PRUNE_KEEP :]
    assert finished_ids(store) == sorted([*kept, fresh])

    for job_id in dropped:
        # All SIX artifacts of the authoritative layout, enumerated...
        for path in job_artifacts(store, job_id):
            assert not path.exists(), path
        # ...and nothing else prefixed with the id either, so a seventh artifact
        # added later fails here rather than leaking orphans.
        assert job_leftovers(store, job_id) == []
        with pytest.raises(ChinamaxError):
            store.resolve_job(job_id)

    index = json.loads(store.index_path.read_text(encoding="utf-8"))["jobs"]
    assert set(dropped).isdisjoint(index), "a pruned id still resolves through state.json"
    assert set(kept) <= set(index)
    for job_id in (*kept, running, interrupted):
        for path in job_artifacts(store, job_id):
            assert path.exists(), path


def test_never_prunes_running(dispatch_env):
    """Active and interrupted Jobs are never pruned and never counted."""
    env = dispatch_env()
    store = env.store.ensure()
    seeded = seed_finished(store, env.workspace)
    running = seed_running(store, env.workspace)
    interrupted = seed_interrupted(store, env.workspace)

    dropped = store.prune()

    assert sorted(dropped) == sorted(seeded[: SEEDED - state.PRUNE_KEEP])
    assert running not in dropped and interrupted not in dropped
    for job_id in (running, interrupted):
        for path in job_artifacts(store, job_id):
            assert path.exists(), path
    # Neither consumed a slot: 50 FINISHED Jobs survive, not 48.
    assert len(finished_ids(store)) == state.PRUNE_KEEP
    assert state.is_active(store.read(running))
    assert state.effective_status(store.read(interrupted)) == state.STATUS_INTERRUPTED
    # An interrupted Job's Thread is exactly what pruning it would destroy.
    assert store.transcript_path(interrupted).exists()
