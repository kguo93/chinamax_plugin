"""Two Jobs in one workspace must not corrupt the store or each other."""

from __future__ import annotations

import json

from chinamax import state
from conftest import REPORT_PAYLOAD, bash_then_report_script, wait_for_status

SECOND_PROFILE = "mimo"


def test_two_jobs_no_corruption(dispatch_env):
    """Both complete, state.json holds BOTH ids, and each record is its own."""
    env = dispatch_env(bash_then_report_script())
    env.bind(bash_then_report_script(), profile=SECOND_PROFILE)
    store = env.store

    first_code, first = env.dispatch(prompt="First job.")
    second_code, second = env.dispatch(prompt="Second job.", profile=SECOND_PROFILE)
    assert (first_code, second_code) == (0, 0)
    assert first != second

    for job_id in (first, second):
        record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
        assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
        assert record["result"] == REPORT_PAYLOAD

    # Parsing is not enough: a lost index update leaves perfectly valid JSON
    # that is simply missing a Job.
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert sorted(index["jobs"]) == sorted([first, second])
    assert index["schemaVersion"] == state.SCHEMA_VERSION

    records = {record["id"]: record for record in store.load_records()[0]}
    assert set(records) == {first, second}
    assert records[first]["title"] == "First job."
    assert records[second]["title"] == "Second job."
    assert records[first]["profile"] != records[second]["profile"]
    assert records[first]["pid"] != records[second]["pid"]
    for job_id in (first, second):
        assert store.transcript_path(job_id).stat().st_size > 0
        assert store.log_path(job_id).stat().st_size > 0
