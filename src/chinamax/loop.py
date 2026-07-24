"""The streaming Messages tool-use loop.

Every ``tool_use`` block in an assistant turn is executed in arrival order and
answered by a ``tool_result`` carrying its ``tool_use_id``; the loop ends only
when ``report_result`` arrives. The transcript is write-ahead: the outgoing
delta is appended and flushed before the API call, the assembled assistant turn
after the stream completes — and only ever after an attempt COMPLETES, since
`liveness` hands nothing back short of ``message_stop``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import anthropic

from chinamax.confinement import ToolContext
from chinamax.liveness import LoopConfig, emit_event, stream_with_ladder
from chinamax.profiles import Profile
from chinamax.spec import JobSpec
from chinamax.tools import REPORT_RESULT, Registry, build_registry
from chinamax.transcript import Transcript, read_repaired_messages

#: The progress-phase vocabulary, CLOSED: a reporter is called with one of these
#: verbatim, and the jobs scope stores it as the Job record's ``phase``.
PHASE_STARTING = "starting"
PHASE_CALLING_MODEL = "calling-model"
PHASE_RUNNING_TOOL = "running-tool"
PHASE_REPORTING = "reporting"
PHASES = (PHASE_STARTING, PHASE_CALLING_MODEL, PHASE_RUNNING_TOOL, PHASE_REPORTING)

#: How much of a tool's input a progress line quotes.
_PREVIEW_LIMIT = 200

SYSTEM_TEMPLATE = """You are a worker model executing one task inside the workspace {workspace}.

{posture}

Every tool is confined to that workspace: the file tools reject any path that \
resolves outside it, and bash runs with {workspace} as its working directory. \
Commands that destroy data or leave the machine are refused, and each one is \
bounded by a timeout whose expiry comes back as an observation rather than \
ending the job.

When the task is finished — whether it succeeded or not — you MUST call the \
report_result tool. That call is the only way to end this job, and its payload is \
relayed back verbatim as the job's result: set outcome to "completed", "blocked" or \
"failed", write a summary, and fill in changed_files, commands_run, tests, failures \
and concerns as far as they apply."""

WRITE_POSTURE = "You may create and modify files in this workspace."
READ_ONLY_POSTURE = (
    "This job is read-only: investigate and report, do not modify this workspace. "
    "The file-editing tools are not available to you and write-shaped shell "
    "commands are refused."
)

NUDGE = (
    "Your last turn used no tools. Keep working with the tools you have, or call the "
    "report_result tool to finish this job — report_result is the only way to end it."
)


def run_loop(
    client: anthropic.Anthropic,
    profile: Profile,
    spec: JobSpec,
    transcript: Transcript,
    config: LoopConfig,
    reporter: Callable[[str, str], None] | None = None,
) -> dict:
    """Drive one Job to its report_result.

    There is no wall-clock bound and no turn bound on this loop (ADR 0002):
    ``report_result``, ladder exhaustion, a permanent provider error and the
    jobs scope's cancel are its only exits.

    Args:
        client: The SDK client built for the Profile.
        profile: The resolved Profile.
        spec: The validated job spec.
        transcript: The Job's open Thread transcript.
        config: The Job's supervision configuration.
        reporter: Called ``(phase, message)`` at every turn boundary and around
            each tool execution, with ``phase`` from `PHASES`. The jobs scope
            mirrors it into the Job's log and record; a callback that raises is
            logged and swallowed.

    Returns:
        The ``report_result`` payload, verbatim.

    Raises:
        RunFailure: On ladder exhaustion or a permanent provider error.
    """
    registry = build_registry(spec.write)
    # The workspace realpath is resolved once per Job: every later containment
    # check compares against it instead of realpathing the root again.
    context = ToolContext(
        root=Path(os.path.realpath(spec.workspace)),
        write=spec.write,
        bash_timeout_s=spec.bash_timeout_s,
    )
    messages = _seed_history(spec)
    _report(
        reporter,
        PHASE_STARTING,
        f"job started on profile {profile.name} in {spec.workspace}"
        + (f", seeded with {len(messages)} prior turn(s)" if messages else ""),
    )
    if not messages:
        _append(transcript, messages, "user", [{"type": "text", "text": spec.prompt}])
    # A seeded history always ENDS in an unsent user turn — the resume
    # normalization folds the follow-up into it — so the first request sends
    # that turn without re-appending it to the transcript, and the spec's prompt
    # is never appended as a second user message. Appending both would deliver
    # the follow-up twice.
    turn_number = 0
    while True:
        turn_number += 1
        _report(reporter, PHASE_CALLING_MODEL, f"turn {turn_number}: calling {profile.model}")
        content = _stream_turn(client, profile, spec, registry, messages, config, transcript)
        _append(transcript, messages, "assistant", content)

        tool_uses = [block for block in content if block.get("type") == "tool_use"]
        if not tool_uses:
            # Keys on the absence of tool_use, never on stop_reason: end_turn,
            # max_tokens and stop_sequence all produce a tool-less turn, and
            # re-sending would repeat the same request forever.
            _report(
                reporter,
                PHASE_CALLING_MODEL,
                f"turn {turn_number}: no tool use; restating the report_result contract",
            )
            _append(transcript, messages, "user", [{"type": "text", "text": NUDGE}])
            continue

        results, payload = _run_tool_uses(tool_uses, registry, context, reporter)
        if payload is not None:
            # Siblings of the terminal report_result are still answered in the
            # durable record, even though no further request is sent.
            if results:
                _append(transcript, messages, "user", results)
            return payload
        _append(transcript, messages, "user", results)


def _seed_history(spec: JobSpec) -> list[dict]:
    """Rehydrate a Thread the caller pre-populated, or start empty.

    Only ``seed_transcript`` opens this path: without it an existing transcript
    is truncated (the fresh-run default), which is what stops a re-run from
    grafting a new conversation onto stale history. The read goes through the
    same tolerant REPAIRING reader a resume uses, because this runs in exactly
    the crash-recovery case where a torn trailing line is expected.

    Args:
        spec: The validated job spec.

    Returns:
        The prior turns, or an empty list for a fresh run.
    """
    if not spec.seed_transcript:
        return []
    try:
        if spec.transcript_path.stat().st_size <= 0:
            return []
        return read_repaired_messages(spec.transcript_path)
    except OSError:
        return []


def _stream_turn(
    client: anthropic.Anthropic,
    profile: Profile,
    spec: JobSpec,
    registry: Registry,
    messages: list[dict],
    config: LoopConfig,
    transcript: Transcript,
) -> list[dict]:
    """Stream one assistant turn through the supervision ladder.

    The ladder replays a snapshot of ``messages`` on every attempt and returns
    only a turn that reached ``message_stop``, so a retried attempt contributes
    nothing here — and nothing to the canonical history the caller appends to.
    """
    message = stream_with_ladder(
        client,
        model=profile.model,
        max_tokens=profile.max_tokens,
        system=_system_prompt(spec),
        tools=registry.schemas,
        messages=messages,
        config=config,
        on_retry=transcript.append_retry,
    )
    return [_block_to_dict(block) for block in message.content]


def _run_tool_uses(
    tool_uses: list[dict],
    registry: Registry,
    context: ToolContext,
    reporter: Callable[[str, str], None] | None = None,
) -> tuple[list[dict], dict | None]:
    """Execute every tool_use block in arrival order.

    Every call goes through the registry, so a name this Job does not carry —
    a write tool in a read-only Job, or one that was never registered — comes
    back as an error observation instead of executing.

    Args:
        tool_uses: The turn's tool_use blocks.
        registry: The Job's posture-filtered registry.
        context: The Job's tool context.
        reporter: The progress reporter, called around each execution.

    Returns:
        The tool_result blocks to send back, and the terminal ``report_result``
        payload when one arrived (the first block wins; later ones are ignored).
    """
    results: list[dict] = []
    payload: dict | None = None
    for block in tool_uses:
        name = block.get("name")
        tool_use_id = block.get("id")
        value = block.get("input")

        if name == REPORT_RESULT:
            problem = registry.validate(name, value)
            if problem is not None:
                results.append(_error_result(tool_use_id, problem))
            elif payload is None:
                # The terminal block is deliberately left unanswered: it IS the terminus.
                payload = value
                _report(
                    reporter,
                    PHASE_REPORTING,
                    f"report_result: outcome={value.get('outcome')!r}",
                )
            continue

        _report(reporter, PHASE_RUNNING_TOOL, f"{name}: {_preview(value)}")
        content, is_error = registry.dispatch(name, value, context)
        _report(
            reporter,
            PHASE_RUNNING_TOOL,
            f"{name}: {'error' if is_error else 'ok'}, {len(content)} chars",
        )
        if is_error:
            results.append(_error_result(tool_use_id, content))
        else:
            results.append(
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
            )
    return results, payload


def _preview(value: object) -> str:
    """Render a tool's input compactly for one progress line."""
    try:
        text = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= _PREVIEW_LIMIT else text[:_PREVIEW_LIMIT] + "…"


def _report(
    reporter: Callable[[str, str], None] | None, phase: str, message: str
) -> None:
    """Deliver one progress event, swallowing a reporter failure.

    An observability failure must never turn otherwise valid model work into a
    Runtime failure, so a callback that raises is logged and dropped.

    Args:
        reporter: The progress reporter, or None when nobody is listening.
        phase: One of `PHASES`.
        message: The event text.
    """
    if reporter is None:
        return
    try:
        reporter(phase, message)
    except Exception as error:  # noqa: BLE001 - see the docstring
        emit_event(
            "warning",
            {"message": f"progress reporter failed: {type(error).__name__}: {error}"},
        )


def _error_result(tool_use_id: str | None, message: str) -> dict:
    """Return a tool_result carrying an error, so no tool_use can kill the Job."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": message,
        "is_error": True,
    }


def _system_prompt(spec: JobSpec) -> str:
    """Render the system prompt for one Job."""
    return SYSTEM_TEMPLATE.format(
        workspace=spec.workspace,
        posture=WRITE_POSTURE if spec.write else READ_ONLY_POSTURE,
    )


def _block_to_dict(block: object) -> dict:
    """Convert an SDK content block to a plain, JSON-serializable dict."""
    data = block.model_dump(mode="json")
    return {key: value for key, value in data.items() if value is not None}


def _append(
    transcript: Transcript, messages: list[dict], role: str, content: list[dict]
) -> None:
    """Record one turn in both the in-memory history and the durable Thread."""
    messages.append({"role": role, "content": content})
    transcript.append_message(role, content)
