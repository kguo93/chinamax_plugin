"""The Bridge's command SEQUENCE, driven directly against the CLI seam.

These tests do not execute the Bridge markdown — they run the exact `python -m
chinamax` verbs the contract tells the Bridge to run, as SUBPROCESSES against the
fake provider, exactly as a Bash-only agent would. That proves the sequence is
sound (dispatch → poll → result, steer mid-run, resume after terminal, and the
byte-safe stdin transport); the contract file's stanzas are checked separately
in `test_bridge_contract.py`, and surface/03's live gauntlet is what actually
exercises the Bridge agent itself.
"""

from __future__ import annotations

import json
import subprocess
import sys

from chinamax import state
from chinamax.transcript import STEER_MARKER, read_messages, write_messages
from conftest import (
    PROFILE,
    REPORT_PAYLOAD,
    aged,
    assert_wire_shape,
    bash_then_report_script,
    build_record,
    report_turn,
    wait_for_status,
)

MODULE = [sys.executable, "-m", "chinamax"]
STEER_MSG = "stop touching module X"
FOLLOW_UP = "Now summarize what you changed."
RACE_MSG = "actually, also update the changelog"


def seam(*args, workspace=None, timeout=90):
    """Run one `python -m chinamax` verb as a subprocess (text mode)."""
    return subprocess.run(
        MODULE + list(args), capture_output=True, text=True, timeout=timeout
    )


def dispatch(env, *extra, prompt=None, prompt_bytes=None, timeout=90):
    """Run the `task` verb as a subprocess, prompt on STDIN; return (proc, job_id)."""
    args = ["task", "--profile", PROFILE, "--workspace", str(env.workspace), *extra]
    if prompt_bytes is not None:
        proc = subprocess.run(
            MODULE + args, input=prompt_bytes, capture_output=True, timeout=timeout
        )
        return proc, proc.stdout.decode().strip()
    proc = subprocess.run(
        MODULE + args,
        input="Do the task." if prompt is None else prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc, proc.stdout.strip()


def gated_dispatch(env, script, gate_index, *, prompt=None, prompt_bytes=None):
    """Dispatch via subprocess, hold the worker at ``gate_index``, return release.

    The provider lives in the pytest process, so its gate holds the detached
    worker between two turns; the caller writes into that window, then releases.
    """
    provider = env.bind(script)
    reached, release = provider.gate(gate_index)
    proc, job_id = dispatch(env, prompt=prompt, prompt_bytes=prompt_bytes)
    stderr = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode()
    assert proc.returncode == 0, stderr
    assert reached.wait(60.0), "worker never reached the gated turn"
    return provider, job_id, release


def seed_thread(store, job_id: str, text: str) -> None:
    """Give a Job a one-turn Thread through the production writer."""
    write_messages(
        state.precreate(store.transcript_path(job_id)),
        [{"role": "user", "content": [{"type": "text", "text": text}]}],
    )


def test_dispatch_poll_relay_sequence(dispatch_env):
    """task → status --wait --timeout-ms (2 while running, 0 terminal) → result.

    The Job detaches (its id returns immediately); a poll while running exits 2
    and surfaces a progress line the Bridge would relay; a poll after terminal
    exits 0; and `result` returns the worker's stored payload verbatim
    (AC bullet 3). This exercises the CLI seam, not the Bridge markdown.
    """
    env = dispatch_env()
    # Gate the SECOND turn, so the log already carries the bash observation when
    # we poll — a progress line the Bridge relays.
    provider, job_id, release = gated_dispatch(env, bash_then_report_script(), 1)
    store = env.store
    ws = str(env.workspace)

    # Detached: the id came back immediately and the record exists and is active.
    assert job_id.startswith("task-")
    assert store.read(job_id)["status"] in (state.STATUS_RUNNING, state.STATUS_QUEUED)

    # A poll WHILE RUNNING: exit 2 (still active), progress surfaced for relay.
    active = seam("status", job_id, "--wait", "--timeout-ms", "500", "--workspace", ws)
    assert active.returncode == 2, active.stderr
    assert job_id in active.stdout
    assert "| " in active.stdout, "a running poll must surface a progress-preview line"
    release.set()

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    # A poll AFTER TERMINAL: exit 0.
    terminal = seam("status", job_id, "--wait", "--timeout-ms", "500", "--workspace", ws)
    assert terminal.returncode == 0, terminal.stderr

    # `result`: exit 0, the worker's stored payload returned.
    got = seam("result", job_id, "--workspace", ws)
    assert got.returncode == 0, got.stderr
    assert REPORT_PAYLOAD["summary"] in got.stdout
    for changed in REPORT_PAYLOAD["changed_files"]:
        assert changed in got.stdout

    # "Verbatim" pinned: `result --json` bytes equal the stored artifact.
    as_json = seam("result", job_id, "--json", "--workspace", ws)
    assert as_json.returncode == 0, as_json.stderr
    assert as_json.stdout == store.result_path(job_id).read_text(encoding="utf-8")
    assert json.loads(as_json.stdout) == REPORT_PAYLOAD


def test_steer_midrun_and_resume_after(dispatch_env):
    """A steer mid-run lands next turn; a follow-up after terminal resumes.

    Also covers the finish-during-steer race: a steer to an already-terminal Job
    reports NOT delivered and points at resume, and the re-route carries the
    ORIGINAL message as the resume prompt — with an EXPLICIT id, so a second
    concurrent Job's Thread is never continued by mistake (AC bullet 4).
    """
    env = dispatch_env()
    ws = str(env.workspace)

    # --- Mid-run steer: held at turn 0, the steer lands in the NEXT request. ---
    provider, job_id, release = gated_dispatch(env, bash_then_report_script(), 0)
    store = env.store

    st = subprocess.run(
        MODULE + ["steer", job_id, "--workspace", ws],
        input=STEER_MSG, capture_output=True, text=True, timeout=60,
    )
    assert st.returncode == 0, st.stderr
    assert store.read(job_id)["status"] == state.STATUS_RUNNING
    release.set()

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")
    carrying = provider.requests[1]["body"]["messages"]
    assert STEER_MARKER + STEER_MSG in json.dumps(carrying), "steer not seen next turn"
    assert_wire_shape(carrying)

    # --- Resume after terminal: an explicit id continues the same Thread. ---
    nxt = env.bind([report_turn()])  # a fresh provider; the first script is spent
    rz = subprocess.run(
        MODULE + ["resume", job_id, "--workspace", ws],
        input=FOLLOW_UP, capture_output=True, text=True, timeout=60,
    )
    assert rz.returncode == 0, rz.stderr
    resumed = rz.stdout.strip()
    assert resumed.startswith("task-") and resumed != job_id
    rec2 = wait_for_status(store, resumed, state.TERMINAL_STATUSES)
    assert rec2["status"] == state.STATUS_COMPLETED, rec2.get("errorMessage")

    sent = nxt.requests[0]["body"]["messages"]
    assert_wire_shape(sent)
    whole = json.dumps(sent)
    assert "Do the task." in whole, "the original prompt was not carried forward"
    assert STEER_MARKER + STEER_MSG in whole, "the mid-run steer was not carried forward"
    assert sent[-1]["role"] == "user"
    assert FOLLOW_UP in json.dumps(sent[-1]), "the follow-up is the newest user turn"

    # --- Finish-during-steer race + explicit-id targeting under concurrency. ---
    older = build_record(
        store, workspace=env.workspace, status=state.STATUS_COMPLETED,
        completed_at=aged(600),
    )
    newer = build_record(
        store, workspace=env.workspace, status=state.STATUS_COMPLETED,
        completed_at=aged(60),
    )
    seed_thread(store, older, "OLDER thread marker.")
    seed_thread(store, newer, "NEWER thread marker.")

    # A steer to a terminal Job: NOT delivered, exit 1, pointing at resume.
    race = subprocess.run(
        MODULE + ["steer", older, "--workspace", ws],
        input=RACE_MSG, capture_output=True, text=True, timeout=60,
    )
    assert race.returncode == 1
    assert "resume" in race.stderr and older in race.stderr

    # Re-route to resume, EXPLICIT older id, original message AS the resume prompt.
    prov3 = env.bind([report_turn()])
    rr = subprocess.run(
        MODULE + ["resume", older, "--workspace", ws],
        input=RACE_MSG, capture_output=True, text=True, timeout=60,
    )
    assert rr.returncode == 0, rr.stderr
    cont = wait_for_status(store, rr.stdout.strip(), state.TERMINAL_STATUSES)
    assert cont["status"] == state.STATUS_COMPLETED, cont.get("errorMessage")

    sent3 = json.dumps(prov3.requests[0]["body"]["messages"])
    assert "OLDER thread marker." in sent3, "the explicit id's Thread must continue"
    assert "NEWER thread marker." not in sent3, "never the concurrent Job's Thread"
    assert RACE_MSG in sent3, "the original message must ride as the resume prompt"


def test_prompt_and_steer_survive_shell_metacharacters(dispatch_env, tmp_path):
    """A prompt and a steer with quotes/newlines/`$(…)`/leading-dash survive stdin.

    They arrive at the fake provider BYTE-IDENTICAL through the stdin transport,
    and NO subshell executes — the `$(touch …)` sentinels are never created.
    """
    env = dispatch_env()
    ws = str(env.workspace)
    prompt_sentinel = tmp_path / "prompt-pwned"
    steer_sentinel = tmp_path / "steer-pwned"
    # Leading dash, embedded quotes, a command substitution, a backtick, and an
    # internal newline and tab — everything argv interpolation would mangle.
    prompt = (
        "-\"quoted\" 'apos' $(touch %s) `id` & done\n"
        "second line | pipe;\ttab" % prompt_sentinel
    )
    steer_msg = (
        "-flag \"dq\" 'sq' $(touch %s) `whoami`\n"
        "line two" % steer_sentinel
    )

    # Prompt over stdin as BYTES (no text-mode newline handling at all).
    provider, job_id, release = gated_dispatch(
        env, bash_then_report_script(), 0, prompt_bytes=prompt.encode("utf-8")
    )
    store = env.store

    st = subprocess.run(
        MODULE + ["steer", job_id, "--workspace", ws],
        input=steer_msg.encode("utf-8"), capture_output=True, timeout=60,
    )
    assert st.returncode == 0, st.stderr.decode()
    release.set()

    record = wait_for_status(store, job_id, state.TERMINAL_STATUSES)
    assert record["status"] == state.STATUS_COMPLETED, record.get("errorMessage")

    # The prompt reached the provider byte-identical, as its first user turn.
    first_user = provider.requests[0]["body"]["messages"][0]
    assert first_user["role"] == "user"
    assert first_user["content"][0]["text"] == prompt

    # The steer reached the provider byte-identical, marker-prefixed, next turn.
    carrying = provider.requests[1]["body"]["messages"][-1]
    steer_texts = [
        block["text"]
        for block in carrying["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    assert STEER_MARKER + steer_msg in steer_texts

    # NO subshell executed on either transport: neither `$(touch …)` ran.
    assert not prompt_sentinel.exists()
    assert not steer_sentinel.exists()
