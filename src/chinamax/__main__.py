"""CLI seam for the Runtime and the Job supervisor.

Verbs: ``exec`` (run one spec in the foreground), ``task`` (dispatch a durable
detached Job and return its id), ``task-worker`` (the detached worker itself),
``status``, ``logs``, ``result``, ``cancel`` and ``resume``. Exit codes are
shared across the Job verbs so the Bridge branches on a code instead of parsing
prose: 0 = the Job is terminal, 2 = the Job is still active, 1 = a usage or
resolution error. The set is TOTAL — every non-terminal return is 2, including
an early wake-up on progress, and a derived-`interrupted` Job returns 0 with the
distinction carried in the OUTPUT rather than in a fourth code.

Nothing here reaps state at a session boundary (ADR 0004).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from chinamax import ChinamaxError, profiles, provider, state
from chinamax.liveness import LoopConfig, RunFailure, build_config, emit_event
from chinamax.loop import PHASE_REPORTING, PHASE_STARTING, run_loop
from chinamax.spec import JobSpec, load_spec, parse_spec
from chinamax.transcript import Transcript

#: How many trailing log lines `status` shows as a Job's progress preview.
PREVIEW_LINES = 4
#: How many finished Jobs a bare `status` lists beside every active one.
RECENT_LIMIT = 20

EXIT_TERMINAL = 0
EXIT_ACTIVE = 2
EXIT_ERROR = 1

#: The `report_result` payload's seven fields, in schema order — the order the
#: default `result` rendering walks them in.
RESULT_FIELDS = (
    "outcome",
    "summary",
    "changed_files",
    "commands_run",
    "tests",
    "failures",
    "concerns",
)
#: What `cancel` records as a cancelled Job's reason.
CANCEL_REASON = "Cancelled by user."
#: What `resume` sends when the operator supplied no follow-up.
DEFAULT_RESUME_PROMPT = "Continue the previous task."
#: A resume target named on argv is recognised by the Job-id prefix; anything
#: else in first position is the start of the prompt.
JOB_ID_PREFIX = "task-"
#: The largest steer message that is queued rather than refused. Beyond this a
#: steer would only trip a provider 400 no drain could recover from.
STEER_MAX_BYTES = 256 * 1024


def execute_spec(
    spec: JobSpec,
    config: LoopConfig | None = None,
    reporter=None,
    steer_dir: Path | None = None,
) -> dict:
    """Run one validated Job to its result.

    The shared entry: sanitizing the environment lives here, not in a verb
    handler, so the `exec` verb and the jobs scope's in-process task-worker both
    get it. The worker calls this directly rather than subprocessing back into
    `exec`, and never re-implements the loop.

    Args:
        spec: The validated job spec.
        config: Supervision configuration to override the defaults with; the
            spec's own overrides are applied on top.
        reporter: The progress reporter handed to the loop.
        steer_dir: The Job's steer queue, drained at each loop boundary; None
            (the `exec` path) disables steering.

    Returns:
        The ``report_result`` payload, verbatim.

    Raises:
        RunFailure: On ladder exhaustion or a permanent provider error.
        ChinamaxError: On any configuration failure.
    """
    provider.sanitize_environment()
    config = build_config(spec, config)
    profile = profiles.resolve_profile(spec.profile)
    client = provider.build_client(
        profile, profiles.resolve_key(profile), config.inactivity_timeout_s
    )
    with Transcript(
        spec.transcript_path, clock=config.clock, append=spec.seed_transcript
    ) as transcript:
        payload = run_loop(
            client, profile, spec, transcript, config, reporter=reporter, steer_dir=steer_dir
        )
    _write_result(spec.result_path, payload)
    return payload


def run_exec(spec_path: str | Path, config: LoopConfig | None = None) -> int:
    """Run one Job from a job-spec file.

    The Runtime owns no state store, so this is also the failure seam: ladder
    exhaustion and a permanent provider error end the run here, as a nonzero
    exit plus one structured JSON line through the progress reporter.

    Args:
        spec_path: Path to the job-spec JSON file.
        config: Supervision configuration to override the defaults with.

    Returns:
        0 once the result has been written, 1 on a terminal provider failure.

    Raises:
        ChinamaxError: On any validation or configuration failure.
    """
    try:
        execute_spec(load_spec(spec_path), config=config)
    except RunFailure as failure:
        emit_event("failure", failure.payload)
        return EXIT_ERROR
    return EXIT_TERMINAL


def run_task(args: argparse.Namespace) -> int:
    """Dispatch a durable Job and return its id immediately.

    Everything that can fail fast — the workspace, the prompt, the Profile —
    fails on stderr BEFORE the record is written, so an unknown Profile never
    becomes a `failed` Job the operator has to go read.

    Args:
        args: The parsed ``task`` arguments.

    Returns:
        0 once the worker is spawned, 1 on a usage or dispatch failure.
    """
    prompt = _read_prompt(args.prompt)
    store = state.open_store(args.workspace)
    profile = profiles.resolve_profile(args.profile)

    job_id = store.reserve_id()
    store.create(
        state.new_record(
            job_id,
            prompt=prompt,
            profile=profile.name,
            write=not args.read_only,
            workspace_root=store.workspace_root,
            log_file=store.log_path(job_id),
            bash_timeout_s=args.bash_timeout_s,
            originating_session=state.session_id(),
        )
    )
    state.make_dir(store.steer_dir(job_id))
    code = _spawn_worker(store, job_id)
    # Pruning runs on dispatch and on terminal transitions only — never on a
    # throttled progress update, which would scan-and-unlink constantly.
    store.prune()
    return code


def run_task_worker(job_id: str, state_dir: str) -> int:
    """Claim a queued Job and run its Runtime loop in-process.

    Args:
        job_id: The Job to run.
        state_dir: Its per-workspace state directory.

    Returns:
        0 when the Job completed (or was already claimed), 1 when it failed.
    """
    store = state.JobStore(Path(state_dir))
    pid = os.getpid()
    claimed = store.claim(
        job_id,
        {
            "status": state.STATUS_RUNNING,
            "startedAt": state.utc_now(),
            "phase": PHASE_STARTING,
            "pid": pid,
            "pidStartTime": state.read_pid_start_time(pid),
        },
    )
    if claimed is None:
        # Two arms, distinguished for an honest log: a TERMINAL record means a
        # cancel landed first, while a non-terminal record `claim` would not
        # reclaim means a LIVE claimant already owns it (a crashed claimant's
        # record IS reclaimed). Either way, exit without running rather than
        # overwriting the record or double-executing the Job.
        current = (store.try_read(job_id) or {}).get("status", "unreadable")
        reason = (
            f"is already {current!r}"
            if current in state.TERMINAL_STATUSES
            else f"is {current!r} with a live worker"
        )
        print(
            f"chinamax: {job_id} {reason}; exiting without running",
            file=sys.stderr,
        )
        return EXIT_TERMINAL

    state.make_dir(store.steer_dir(job_id))
    transcript_path = state.precreate(store.transcript_path(job_id))
    result_path = state.precreate(store.result_path(job_id))
    reporter = state.ProgressReporter(store, job_id)
    heartbeat = state.Heartbeat(reporter)
    heartbeat.start()
    changes: dict = {}
    try:
        payload = execute_spec(
            _worker_spec(job_id, claimed.get("request") or {}, transcript_path, result_path),
            reporter=reporter,
            steer_dir=store.steer_dir(job_id),
        )
        changes = {
            "status": state.STATUS_COMPLETED,
            "result": _fold_result(result_path, payload),
        }
    except RunFailure as failure:
        # The full JSON payload lands in the Job log; the record carries the
        # compact rendering.
        reporter(
            PHASE_REPORTING,
            json.dumps({"event": "failure", **failure.payload}, sort_keys=True),
        )
        changes = {
            "status": state.STATUS_FAILED,
            "errorMessage": state.render_failure(failure.payload),
        }
    except Exception as error:  # noqa: BLE001 - any escape ends the Job honestly
        message = f"{type(error).__name__}: {error}"
        reporter(PHASE_REPORTING, f"job failed: {message}")
        changes = {"status": state.STATUS_FAILED, "errorMessage": message}
    finally:
        # Stopped AND JOINED before the terminal write, so a heartbeat in flight
        # can never land after — and resurrect `running` over — a terminal record.
        heartbeat.stop()
        state.secure_file(result_path)
        reporter.close()

    changes["phase"] = PHASE_REPORTING
    changes["completedAt"] = state.utc_now()
    if store.update(job_id, changes, expect={state.STATUS_RUNNING}) is None:
        print(
            f"chinamax: {job_id} left `running` before its terminal write",
            file=sys.stderr,
        )
    store.prune()
    return EXIT_TERMINAL if changes["status"] == state.STATUS_COMPLETED else EXIT_ERROR


def run_status(args: argparse.Namespace) -> int:
    """Show running and recent Jobs, or one Job (optionally waiting on change).

    Args:
        args: The parsed ``status`` arguments.

    Returns:
        0 for a terminal Job or a bare listing, 2 while a named Job is active.
    """
    store = state.open_store(args.workspace)
    if args.job is None:
        return _status_list(store)
    job_id = store.resolve_job(args.job)
    if args.wait:
        return _status_wait(store, job_id, args.timeout_ms)
    record = store.read(job_id)
    _print_job(store, record)
    return _exit_code(record)


def run_logs(args: argparse.Namespace) -> int:
    """Print a Job's timestamped progress log, and its spawn log when empty.

    The spawn log is keyed on the progress log being EMPTY, never on status
    `failed`: the crash it exists for — a worker that dies on import, before it
    can write anything — leaves the record `queued`.

    Args:
        args: The parsed ``logs`` arguments.

    Returns:
        0 for a terminal Job, 2 while it is active.
    """
    store = state.open_store(args.workspace)
    job_id = store.resolve_job(args.job)

    text = _read_text(store.log_path(job_id))
    if text and args.tail is not None:
        lines = text.splitlines()[-args.tail :] if args.tail > 0 else []
        text = "".join(line + "\n" for line in lines)
    if text.strip():
        sys.stdout.write(text)
    else:
        spawn = _read_text(store.spawn_log_path(job_id))
        if spawn:
            print(f"--- spawn log: {store.spawn_log_path(job_id)} ---")
            sys.stdout.write(spawn if spawn.endswith("\n") else spawn + "\n")
    return _exit_code(store.read(job_id))


def run_result(args: argparse.Namespace) -> int:
    """Print a finished Job's stored result, refusing an active one.

    Only `completed` carries a payload: every `report_result` ends a Job as
    `completed` with the payload stored verbatim, even when its own ``outcome``
    is ``failed`` or ``blocked``, while status `failed` is reserved for Runtime
    errors that carry only an ``errorMessage``. So this reports honestly per
    state rather than inventing an empty payload.

    Args:
        args: The parsed ``result`` arguments.

    Returns:
        0 for any terminal Job — with or without a payload, the difference lives
        in the output — 2 while it is still active.
    """
    store = state.open_store(args.workspace)
    record = _result_target(store, args.job)
    status = state.effective_status(record)
    if status in state.ACTIVE_STATUSES:
        print(
            f"chinamax: {record['id']} is {status}, not finished; "
            f"follow it with `status {record['id']}`",
            file=sys.stderr,
        )
        return EXIT_ACTIVE
    if args.json:
        sys.stdout.write(_result_json(store, record, status))
    else:
        _print_result(record, status)
    return EXIT_TERMINAL


def run_cancel(args: argparse.Namespace) -> int:
    """Stop a Job's whole process tree and mark the record cancelled.

    The status write goes through the single locked compare-and-swap updater and
    applies only while the record is still non-terminal, so a worker that
    completed during the kill KEEPS its result. A not-yet-started Job is marked
    cancelled directly, which is safe because the worker's own claim is the SAME
    locked CAS: the two compete for one lock and the loser exits.

    Args:
        args: The parsed ``cancel`` arguments.

    Returns:
        0 once the Job is terminal, 1 when nothing could be resolved or when a
        targeted process outlived the SIGKILL sweep.
    """
    store = state.open_store(args.workspace)
    record = _cancel_target(store, args.job)
    job_id = record["id"]
    if record["status"] in state.TERMINAL_STATUSES:
        print(f"{job_id}  {record['status']}  (already terminal; nothing to cancel)")
        return EXIT_TERMINAL

    pid = state.usable_pid(record)
    if pid is not None:
        survivors = state.terminate_tree(pid, record.get("pidStartTime"))
        if survivors:
            # Never write `cancelled` over a Job that was not actually stopped.
            print(
                f"chinamax: {job_id} NOT cancelled: still alive after SIGKILL: "
                f"{', '.join(str(one) for one in survivors)}",
                file=sys.stderr,
            )
            return EXIT_ERROR

    cancelled = store.update(
        job_id,
        {
            "status": state.STATUS_CANCELLED,
            "errorMessage": CANCEL_REASON,
            # Keeps a cancelled Job orderable by the shared "latest" key and
            # freezes its elapsed time in `status`.
            "completedAt": state.utc_now(),
        },
        expect=state.ACTIVE_STATUSES,
    )
    if cancelled is None:
        current = store.read(job_id)
        print(f"{job_id}  {current['status']}  (finished before the cancel landed)")
        return EXIT_TERMINAL
    store.prune()
    print(f"{job_id}  {state.STATUS_CANCELLED}  {CANCEL_REASON}")
    return EXIT_TERMINAL


def run_resume(args: argparse.Namespace) -> int:
    """Dispatch a new Job continuing a finished Job's Thread.

    Args:
        args: The parsed ``resume`` arguments.

    Returns:
        0 once the worker is spawned, 1 on a refusal or a dispatch failure.
    """
    store = state.open_store(args.workspace)
    selector, words = _split_resume_args(args.args)
    prompt = _read_prompt(words, default=DEFAULT_RESUME_PROMPT)
    record = store.create_resume(
        selector, prompt, originating_session=state.session_id()
    )
    job_id = record["id"]
    state.make_dir(store.steer_dir(job_id))
    code = _spawn_worker(store, job_id)
    store.prune()
    return code


def run_steer(args: argparse.Namespace) -> int:
    """Queue a mid-run steer for a running Job, refusing a finished one.

    Leans entirely on jobs/02's `effective_status`: a `queued` or effectively-
    `running` Job is steerable; the three terminal states AND an effectively-
    `interrupted` Job are refused with a pointer to resume, since the only thing
    that would ever drain an interrupted Job's queue is an internal relaunch,
    and resume deliberately does not carry pending steers forward.

    Enqueue and the worker's terminal transition are not synchronized, so after
    the file lands the record is re-read: a Job that went terminal meanwhile is
    reported as NOT delivered rather than a false success. This narrows the race
    the worker's terminal sweep leaves an auditable trace for; it cannot close it.

    Args:
        args: The parsed ``steer`` arguments.

    Returns:
        0 once the steer is queued and the Job is still steerable, 1 on a refusal
        (unknown/ambiguous id, a finished Job, an empty or oversize message, or a
        Job that finished before the steer could be delivered).
    """
    store = state.open_store(args.workspace)
    job_id = store.resolve_job(args.job)
    status = state.effective_status(store.read(job_id))
    if status not in state.ACTIVE_STATUSES:
        raise ChinamaxError(_steer_refusal(job_id, status))

    message = _read_steer_message(args.message)
    steer_id = store.enqueue_steer(job_id, message)

    after = state.effective_status(store.read(job_id))
    if after not in state.ACTIVE_STATUSES:
        raise ChinamaxError(
            f"{job_id} went {after} before the steer {steer_id} could be delivered; "
            f"it was NOT applied — continue its Thread with `resume {job_id} <prompt>`"
        )
    print(f"{job_id}  steer queued  {steer_id}")
    return EXIT_TERMINAL


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Deliberately exposed: it is the surface a test reads to prove no verb takes
    a session id as a selector (ADR 0004).

    Returns:
        The parser for every verb.
    """
    parser = argparse.ArgumentParser(prog="chinamax", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    exec_parser = subcommands.add_parser("exec", help="run one Job from a job spec")
    exec_parser.add_argument("spec_path", help="path to the job-spec JSON file")

    task_parser = subcommands.add_parser("task", help="dispatch a durable detached Job")
    task_parser.add_argument("--profile", required=True, help="the Profile to run against")
    task_parser.add_argument(
        "--read-only", action="store_true", help="opt out of write-capable tools"
    )
    task_parser.add_argument("--workspace", default=None, help="workspace root (default: cwd)")
    task_parser.add_argument(
        "--bash-timeout-s", type=float, default=None, help="per-command bash timeout"
    )
    task_parser.add_argument(
        "prompt", nargs="*", help="the prompt; read from stdin when absent"
    )

    status_parser = subcommands.add_parser("status", help="show running and recent Jobs")
    status_parser.add_argument("job", nargs="?", default=None, help="job id or prefix")
    status_parser.add_argument(
        "--wait", action="store_true", help="block until the Job changes"
    )
    status_parser.add_argument(
        "--timeout-ms",
        type=int,
        default=state.WAIT_TIMEOUT_MS,
        help=f"wait bound in ms, clamped to {state.WAIT_TIMEOUT_MS}",
    )
    status_parser.add_argument("--workspace", default=None, help="workspace root (default: cwd)")

    logs_parser = subcommands.add_parser("logs", help="print a Job's progress log")
    logs_parser.add_argument("job", help="job id or prefix")
    logs_parser.add_argument("--tail", type=int, default=None, help="only the last N lines")
    logs_parser.add_argument("--workspace", default=None, help="workspace root (default: cwd)")

    result_parser = subcommands.add_parser("result", help="print a finished Job's result")
    result_parser.add_argument(
        "job", nargs="?", default=None, help="job id or prefix (default: latest completed)"
    )
    result_parser.add_argument(
        "--json", action="store_true", help="emit the stored result artifact as JSON"
    )
    result_parser.add_argument("--workspace", default=None, help="workspace root (default: cwd)")

    cancel_parser = subcommands.add_parser("cancel", help="stop a Job's process tree")
    cancel_parser.add_argument(
        "job", nargs="?", default=None, help="job id or prefix (default: the active Job)"
    )
    cancel_parser.add_argument("--workspace", default=None, help="workspace root (default: cwd)")

    resume_parser = subcommands.add_parser("resume", help="continue a finished Job's Thread")
    resume_parser.add_argument("--workspace", default=None, help="workspace root (default: cwd)")
    resume_parser.add_argument(
        "args",
        nargs="*",
        help=(
            "an optional job id or prefix, then the follow-up prompt; the prompt "
            "is read from stdin when argv carries none"
        ),
    )

    steer_parser = subcommands.add_parser("steer", help="queue a mid-run message for a running Job")
    steer_parser.add_argument("job", help="job id or prefix")
    steer_parser.add_argument("--workspace", default=None, help="workspace root (default: cwd)")
    steer_parser.add_argument(
        "message", nargs="*", help="the steer message; read from stdin when absent"
    )

    worker_parser = subcommands.add_parser("task-worker", help="run a dispatched Job (internal)")
    worker_parser.add_argument("--job-id", required=True, help="the Job to run")
    worker_parser.add_argument("--state-dir", required=True, help="its state directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the requested verb.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code.
    """
    args = build_parser().parse_args(argv)
    try:
        if args.command == "exec":
            return run_exec(args.spec_path)
        if args.command == "task":
            return run_task(args)
        if args.command == "task-worker":
            return run_task_worker(args.job_id, args.state_dir)
        if args.command == "status":
            return run_status(args)
        if args.command == "logs":
            return run_logs(args)
        if args.command == "result":
            return run_result(args)
        if args.command == "cancel":
            return run_cancel(args)
        if args.command == "resume":
            return run_resume(args)
        if args.command == "steer":
            return run_steer(args)
        raise ChinamaxError(f"unknown command {args.command!r}")
    except ChinamaxError as error:
        print(f"chinamax: {error}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as error:  # provider/tool failures end the Job non-zero
        print(f"chinamax: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_ERROR


def _spawn_worker(store: state.JobStore, job_id: str) -> int:
    """Spawn the detached worker and record its pid.

    Failures BEFORE the child exists — opening the spawn log, building argv,
    `Popen` itself — write status `failed` and exit non-zero. The pid write is
    deliberately NOT in that class: it applies only while the record is still
    `queued`, so a fast worker that already claimed (or finished) makes the
    compare-and-swap fail harmlessly. Marking a Job failed because bookkeeping
    lost a race would strand a live worker under a terminal record.

    Args:
        store: The Job's store.
        job_id: The Job to run.

    Returns:
        0 once the child exists, 1 when the spawn itself failed.
    """
    try:
        spawn_log = state.precreate(store.spawn_log_path(job_id))
        handle = os.open(spawn_log, os.O_WRONLY | os.O_APPEND)
        try:
            child = subprocess.Popen(
                [
                    state.worker_python(),
                    # -P and the neutral cwd stop a workspace that happens to
                    # contain a `chinamax` module from shadowing the package.
                    "-P",
                    "-m",
                    "chinamax",
                    "task-worker",
                    "--job-id",
                    job_id,
                    "--state-dir",
                    str(store.path),
                ],
                cwd=str(store.path),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                close_fds=True,
            )
        finally:
            os.close(handle)
    except Exception as error:  # noqa: BLE001 - no child exists, so the Job is dead
        message = f"dispatch failed: {type(error).__name__}: {error}"
        store.update(
            job_id,
            {
                "status": state.STATUS_FAILED,
                "errorMessage": message,
                "completedAt": state.utc_now(),
            },
            expect={state.STATUS_QUEUED},
        )
        print(f"chinamax: {message}", file=sys.stderr)
        return EXIT_ERROR

    if (
        store.update(
            job_id,
            {"pid": child.pid, "pidStartTime": state.read_pid_start_time(child.pid)},
            expect={state.STATUS_QUEUED},
        )
        is None
    ):
        print(
            f"chinamax: worker for {job_id} claimed its record before the pid write",
            file=sys.stderr,
        )
    print(job_id)
    return EXIT_TERMINAL


def _worker_spec(
    job_id: str, request: dict, transcript_path: Path, result_path: Path
) -> JobSpec:
    """Rebuild runtime/01's frozen job spec from a Job record.

    It goes through `spec.py` validation on this path too, so a record left
    half-written by a crash fails with a named field rather than a confusing
    crash inside the loop.

    Args:
        job_id: The Job (provenance only).
        request: The record's ``request`` block.
        transcript_path: The Job's Thread transcript.
        result_path: The Job's result artifact.

    Returns:
        The validated spec.

    Raises:
        ChinamaxError: Naming the offending field.
    """
    data: dict = {
        "workspace": request.get("workspaceRoot"),
        "profile": request.get("profile"),
        "prompt": request.get("prompt"),
        "transcript_path": str(transcript_path),
        "result_path": str(result_path),
        "job_id": job_id,
    }
    # `write` is carried separately: False is meaningful and must not be dropped.
    present = {key: value for key, value in data.items() if value is not None}
    present["write"] = bool(request.get("write", True))
    # Set on EVERY worker spec: a pre-populated transcript — a resume copy, or a
    # relaunch on a crashed Job — is seeded rather than truncated by the
    # fresh-run default. An empty transcript seeds to nothing and runs fresh.
    present["seed_transcript"] = True
    timeout = request.get("bashTimeoutSec")
    if timeout is not None:
        present["bash_timeout_s"] = timeout
    return parse_spec(present)


def _fold_result(result_path: Path, payload: dict) -> dict:
    """Fold the Runtime's result artifact into the record.

    After this the record is the authoritative copy and the file is only the
    Runtime's write-side artifact; the in-memory payload is the fallback when
    the file cannot be read back.
    """
    try:
        stored = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return payload
    return stored if isinstance(stored, dict) else payload


def _read_prompt(
    words: list[str],
    default: str | None = None,
    subject: str = "task",
    noun: str = "prompt",
) -> str:
    """Return a verb's argv-or-stdin text.

    stdin is read ONLY when argv carries no text and stdin is not a TTY —
    otherwise a bare ``task --profile x`` at a terminal would hang forever on a
    read. Empty text is refused before any record or file is written, unless the
    verb has a default (`resume` does). `steer` reuses this exact shape.

    Args:
        words: The positional words.
        default: What omitted text means, or None to refuse it.
        subject: The verb name, for the empty-input error.
        noun: What the text is called, for the empty-input error.

    Returns:
        The text.

    Raises:
        ChinamaxError: If nothing was supplied and the verb has no default.
    """
    text = " ".join(words).strip()
    if not text and sys.stdin is not None and not sys.stdin.isatty():
        try:
            text = sys.stdin.read().strip()
        except OSError:
            # An unreadable stdin carries no text; it is not a distinct error.
            text = ""
    if text:
        return text
    if default is not None:
        return default
    raise ChinamaxError(
        f"{subject} needs a {noun}: pass it as arguments (after `--`) or on stdin"
    )


def _read_steer_message(words: list[str]) -> str:
    """Return the steer text from argv or stdin, refusing empty or oversize.

    Empty or whitespace-only input, and a message beyond `STEER_MAX_BYTES`, are
    both refused BEFORE any file is written — an oversize steer would otherwise
    be queued only to trip a provider 400 no drain could recover from.

    Args:
        words: The positional message words.

    Returns:
        The steer text.

    Raises:
        ChinamaxError: On empty or oversize input.
    """
    message = _read_prompt(words, subject="steer", noun="message")
    size = len(message.encode("utf-8"))
    if size > STEER_MAX_BYTES:
        raise ChinamaxError(
            f"steer message is too large ({size} bytes > {STEER_MAX_BYTES}-byte cap); "
            f"shorten it, or use `resume` with the full context once the Job finishes"
        )
    return message


def _steer_refusal(job_id: str, status: str) -> str:
    """Build the refusal for steering a Job that is not steerable."""
    reason = (
        "its worker is gone"
        if status == state.STATUS_INTERRUPTED
        else f"it is {status}, not running"
    )
    return (
        f"{job_id} cannot be steered ({reason}) — continue its Thread with "
        f"`resume {job_id} <prompt>`"
    )


def _split_resume_args(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Split ``resume``'s positionals into an optional target and the prompt.

    A first token beginning ``task-`` is the target; anything else in first
    position starts the prompt, and the target then defaults to the most recent
    non-active Job with a Thread.

    Args:
        tokens: The positional arguments.

    Returns:
        The selector (or None) and the prompt words.
    """
    if tokens and tokens[0].startswith(JOB_ID_PREFIX):
        return tokens[0], tokens[1:]
    return None, list(tokens)


def _result_target(store: state.JobStore, selector: str | None) -> dict:
    """Resolve which Job `result` reports on.

    An explicit id or prefix resolves across ALL records through the shared
    resolver; with no id the latest `completed` Job wins.

    Raises:
        ChinamaxError: On a resolution failure, or when nothing has completed.
    """
    if selector is not None:
        return store.read(store.resolve_job(selector))
    records, _ = store.load_records()
    completed = [
        record for record in records if record["status"] == state.STATUS_COMPLETED
    ]
    if not completed:
        raise ChinamaxError(
            "no completed Job in this workspace; name one, or run `status`"
        )
    return max(completed, key=state.latest_key)


def _cancel_target(store: state.JobStore, selector: str | None) -> dict:
    """Resolve which Job `cancel` stops.

    An explicit id or prefix resolves across ALL records; with no id the single
    active Job wins, and several active Jobs are a refusal listing them rather
    than a guess.

    Raises:
        ChinamaxError: On a resolution failure, no active Job, or an ambiguity.
    """
    if selector is not None:
        return store.read(store.resolve_job(selector))
    records, _ = store.load_records()
    active = sorted(
        (record for record in records if state.is_active(record)),
        key=lambda record: record["id"],
    )
    if not active:
        raise ChinamaxError("no active Job to cancel in this workspace")
    if len(active) > 1:
        listed = ", ".join(record["id"] for record in active)
        raise ChinamaxError(f"several Jobs are active; name the one to cancel: {listed}")
    return active[0]


def _result_json(store: state.JobStore, record: dict, status: str) -> str:
    """Return the bytes `result --json` emits.

    The stored ``jobs/<id>.result.json`` is emitted AS STORED rather than
    re-serialized from a parsed value; a terminal Job with no payload gets the
    status and errorMessage instead.
    """
    if status == state.STATUS_COMPLETED:
        stored = _read_text(store.result_path(record["id"]))
        if stored.strip():
            return stored if stored.endswith("\n") else stored + "\n"
        payload = record.get("result")
        if isinstance(payload, dict):
            return json.dumps(payload, sort_keys=True, indent=2) + "\n"
    document = {"status": status, "errorMessage": record.get("errorMessage")}
    return json.dumps(document, sort_keys=True, indent=2) + "\n"


def _print_result(record: dict, status: str) -> None:
    """Render a terminal Job's result: the payload, or the status and reason."""
    print(f"{record['id']}  {status}")
    payload = record.get("result") if status == state.STATUS_COMPLETED else None
    if isinstance(payload, dict):
        for field in RESULT_FIELDS:
            if field not in payload:
                continue
            value = payload[field]
            if not isinstance(value, list):
                print(f"{field}: {value}")
            elif not value:
                print(f"{field}: (none)")
            else:
                print(f"{field}:")
                for item in value:
                    print(f"  - {item}")
        return
    if record.get("errorMessage"):
        print(f"error: {state.escape_control(record['errorMessage'])}")
    if status == state.STATUS_INTERRUPTED:
        print(
            "hint: the worker is gone and this Job will not progress; continue "
            f"its Thread with `resume {record['id']} <prompt>`"
        )


def _status_list(store: state.JobStore) -> int:
    """List every active Job plus the most recent finished ones, each once."""
    records, malformed = store.load_records()
    if malformed:
        print(
            f"chinamax: {len(malformed)} malformed Job record(s): {', '.join(malformed)}",
            file=sys.stderr,
        )
    ordered = sorted(records, key=_updated_at, reverse=True)
    shown = [record for record in ordered if state.is_active(record)]
    seen = {record["id"] for record in shown}
    for record in ordered[:RECENT_LIMIT]:
        if record["id"] not in seen:
            shown.append(record)
            seen.add(record["id"])
    for record in sorted(shown, key=_updated_at, reverse=True):
        _print_job(store, record)
    return EXIT_TERMINAL


def _status_wait(store: state.JobStore, job_id: str, timeout_ms: int) -> int:
    """Block until a Job's status, phase or log changes, or the bound expires.

    The snapshot is ``(status, phase, log size + inode)``; polling is every
    `state.POLL_INTERVAL_S` on a MONOTONIC clock. An already-terminal Job
    returns immediately — and so does an effectively-`interrupted` one, which
    will never leave that state on its own, so a poll-relay that kept waiting
    would hang for the life of the Bridge.
    """
    bound_s = min(max(timeout_ms, 0), state.WAIT_TIMEOUT_MS) / 1000.0
    record = store.read(job_id)
    if state.is_active(record):
        log_path = store.log_path(job_id)
        snapshot = (record["status"], record["phase"], state.log_signature(log_path))
        deadline = time.monotonic() + bound_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(state.POLL_INTERVAL_S, remaining))
            record = store.read(job_id)
            if (
                record["status"],
                record["phase"],
                state.log_signature(log_path),
            ) != snapshot or not state.is_active(record):
                break
    _print_job(store, record)
    return _exit_code(record)


def _print_job(store: state.JobStore, record: dict) -> None:
    """Print one Job's summary line and its progress preview.

    The rendered status is the EFFECTIVE one, so a crashed worker's Job reads
    `interrupted` while the record itself still says `running` — stale detection
    never rewrites a record behind a worker.
    """
    print(
        "  ".join(
            [
                str(record["id"]),
                f"{state.effective_status(record):<11}",
                f"{record['phase'] or '-':<14}",
                f"{_elapsed(record):>8}",
                str(record["profile"]),
                str(record["title"]),
            ]
        )
    )
    if record.get("errorMessage"):
        print(f"    error: {state.escape_control(record['errorMessage'])}")
    for line in state.tail_lines(store.log_path(record["id"]), PREVIEW_LINES):
        print(f"    | {line}")


def _exit_code(record: dict) -> int:
    """Map a Job's status onto the shared exit-code convention.

    An `interrupted` Job returns 0, not 2: it will never leave that state on its
    own, and a Bridge told 2 would poll a dead worker until the operator gave up.
    """
    return EXIT_ACTIVE if state.is_active(record) else EXIT_TERMINAL


def _updated_at(record: dict) -> float:
    """Sort key: a record's ``updatedAt`` as epoch seconds."""
    return state.parse_timestamp(record.get("updatedAt")) or 0.0


def _elapsed(record: dict) -> str:
    """Render a Job's elapsed time.

    Measured from ``startedAt`` once running and from ``createdAt`` while
    queued, and frozen at ``completedAt`` once terminal.
    """
    start = state.parse_timestamp(record.get("startedAt")) or state.parse_timestamp(
        record.get("createdAt")
    )
    if start is None:
        return "-"
    end = state.parse_timestamp(record.get("completedAt"))
    if end is None:
        end = time.time()
    seconds = int(max(0.0, end - start))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _read_text(path: Path) -> str:
    """Read a log file, tolerating absence and arbitrary bytes."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _write_result(path: Path, payload: dict) -> None:
    """Write the verbatim report_result payload atomically.

    Args:
        path: The result path from the job spec.
        payload: The worker's self-report, stored without normalization.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


if __name__ == "__main__":
    sys.exit(main())
