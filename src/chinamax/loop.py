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

from chinamax import state
from chinamax.confinement import ToolContext
from chinamax.liveness import LoopConfig, emit_event, stream_with_ladder
from chinamax.policy import Policy, translate_tool
from chinamax.profiles import Profile
from chinamax.spec import JobSpec
from chinamax.tools import REPORT_RESULT, Registry, build_registry
from chinamax.transcript import (
    STEER_MARKER,
    Transcript,
    read_repaired_messages,
    read_steer_ids,
)

#: The progress-phase vocabulary, CLOSED: a reporter is called with one of these
#: verbatim, and the jobs scope stores it as the Job record's ``phase``.
PHASE_STARTING = "starting"
PHASE_CALLING_MODEL = "calling-model"
PHASE_RUNNING_TOOL = "running-tool"
PHASE_REPORTING = "reporting"
PHASES = (PHASE_STARTING, PHASE_CALLING_MODEL, PHASE_RUNNING_TOOL, PHASE_REPORTING)

#: How much of a tool's input a progress line quotes.
_PREVIEW_LIMIT = 200
#: How much of a steer a drain's log line quotes.
_STEER_EXCERPT_LIMIT = 120
#: Test seam: when set, the drain hard-exits between the transcript flush and the
#: rename, reproducing a crash in that exact window with the steer flushed to the
#: Thread but still queued. Test-only; nothing production reads it.
STEER_ABORT_VARIABLE = "CHINAMAX_STEER_ABORT"

SYSTEM_TEMPLATE = """You are a worker model executing one task inside a confined workspace.

Every tool is confined to that workspace: the file tools reject any path that \
resolves outside it, and bash runs with the workspace as its working directory. \
Commands that destroy data or leave the machine are refused, and each one is \
bounded by a timeout whose expiry comes back as an observation rather than \
ending the job.

When the task is finished — whether it succeeded or not — you MUST call the \
report_result tool. That call is the only way to end this job, and its response \
field is relayed back verbatim, in full, as the job's result: put your complete \
final answer in response — everything the operator should read, exactly as you \
want them to read it. It is your final message, not a summary of one.

Your workspace is {workspace}.

{posture}"""

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
    steer_dir: Path | None = None,
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
        steer_dir: The Job's steer queue, drained at each iteration boundary;
            None (the ``exec`` path) disables steering entirely.

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
    # The dedupe set is read ONCE at startup and updated as the loop drains, so a
    # long Job never re-parses a growing JSONL at every boundary. Seeding it from
    # the transcript is what makes the drain's skip sound: on a relaunch the ids
    # of steers a prior run already injected are here, so they are acknowledged
    # rather than delivered twice — and the worker's transcript seeding is what
    # keeps a skipped-but-not-yet-consumed steer at exactly one delivery.
    consumed = read_steer_ids(spec.transcript_path) if steer_dir is not None else set()
    # Discover the Job's Host policy ONCE (hooks/Memory/MCP config — no subprocess,
    # no server spawn yet). The already-injected Memory set is derived ONCE from
    # the replayed transcript, so lazy injection stays exactly-once across resumes.
    policy = Policy.build(spec, reporter=reporter)
    injected = policy.derive_injected(messages)
    _report(
        reporter,
        PHASE_STARTING,
        f"job started on profile {profile.name} in {spec.workspace}"
        + (f", seeded with {len(messages)} prior turn(s)" if messages else ""),
    )
    # A seeded history always ENDS in an unsent user turn — the resume
    # normalization folds the follow-up into it — so the first request sends
    # that turn without re-appending it to the transcript, and the spec's prompt
    # is never appended as a second user message. Appending both would deliver
    # the follow-up twice.
    turn_number = 0
    stop_hook_active = False
    try:
        # Spawn stdio MCP servers INSIDE the try, so the same finally that hosts
        # the undelivered-steer sweep tears them down even when startup fails
        # midway. The tools array is built ONCE and reused every turn — same
        # object → byte-stable prefix (ADR 0001).
        policy.start_mcp()
        tools = [*registry.schemas, *policy.mcp_schemas()]
        if not messages:
            # Fresh Job: Memory injection is PREPENDED to the first user turn (never
            # the system prompt), so it rides the transcript like any message and
            # is replay/resume-safe. Freshness is empty seeded history, not a flag.
            first_turn: list[dict] = []
            memory_block = policy.first_turn_memory_block(injected)
            if memory_block:
                first_turn.append({"type": "text", "text": memory_block})
            first_turn.append({"type": "text", "text": spec.prompt})
            _append(transcript, messages, "user", first_turn)
        while True:
            # Drain at the top of EVERY iteration, including the first, so a steer
            # written before the worker started lands before the first API call.
            _drain_steers(steer_dir, consumed, transcript, messages, reporter)
            turn_number += 1
            _report(reporter, PHASE_CALLING_MODEL, f"turn {turn_number}: calling {profile.model}")
            content, usage = _stream_turn(
                client, profile, spec, registry, messages, config, transcript, tools
            )
            _append(transcript, messages, "assistant", content)
            _emit_usage(reporter, turn_number, usage)

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

            results, payload, stop_fired = _run_tool_uses(
                tool_uses, registry, context, reporter, policy, injected, stop_hook_active
            )
            # Subsequent Stop firings carry stop_hook_active: true (ADR 0016).
            if stop_fired:
                stop_hook_active = True
            if payload is not None:
                # Siblings of the terminal report_result are still answered in the
                # durable record, even though no further request is sent.
                if results:
                    _append(transcript, messages, "user", results)
                return payload
            _append(transcript, messages, "user", results)
    finally:
        # At the terminal transition, log any steer that lost the accept-versus-
        # terminate race as undelivered — an auditable trace, no delivery change —
        # and tear down every MCP server, even on a startup failure.
        _sweep_undelivered(steer_dir, reporter)
        policy.close()


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


def _drain_steers(
    steer_dir: Path | None,
    consumed: set[str],
    transcript: Transcript,
    messages: list[dict],
    reporter: Callable[[str, str], None] | None,
) -> None:
    """Inject every queued steer into the outgoing user turn, in write order.

    The order is load-bearing: commit the steer to the transcript FIRST (batch
    flushed), acknowledge it into ``consumed/`` SECOND. Renaming first would lose
    the steer outright to a crash in the gap — it would then be in neither the
    queue nor the transcript. A steer whose id already appears in the transcript
    (a crash after the flush but before the rename) is skipped and only
    acknowledged, never injected twice.

    Args:
        steer_dir: The Job's steer queue, or None when steering is disabled.
        consumed: The in-memory set of already-injected steer ids; updated here.
        transcript: The Job's open Thread transcript.
        messages: The in-memory history; the steer rides onto its outgoing turn.
        reporter: The progress reporter, for the drain's log line.
    """
    if steer_dir is None:
        return
    consumed_subdir = steer_dir / state.STEER_CONSUMED_DIRNAME
    for path in state.list_steer_files(steer_dir):
        steer_id = path.name
        if steer_id in consumed:
            _consume_steer(path, consumed_subdir)
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        marked = STEER_MARKER + raw
        # An additional text block on the user message already carrying this
        # turn's tool_results, never a second consecutive user message — third-
        # party endpoints may 400 on those. On the first iteration that message
        # is the original prompt, and the steer rides it the same way.
        _attach_steer(messages, {"type": "text", "text": marked})
        transcript.append_steer(marked, steer_id, state.steer_enqueued_at(steer_id))
        consumed.add(steer_id)
        _report(reporter, PHASE_CALLING_MODEL, f"steer {steer_id} drained: {_steer_excerpt(raw)}")
        _steer_abort_point()
        _consume_steer(path, consumed_subdir)


def _sweep_undelivered(
    steer_dir: Path | None, reporter: Callable[[str, str], None] | None
) -> None:
    """Log the ids of any steers still queued at the terminal transition."""
    if steer_dir is None:
        return
    for path in state.list_steer_files(steer_dir):
        _report(
            reporter,
            PHASE_REPORTING,
            f"steer {path.name} undelivered: the job ended before it was drained",
        )


def _attach_steer(messages: list[dict], block: dict) -> None:
    """Ride a steer text block onto the outgoing user turn, or start one."""
    if messages and messages[-1].get("role") == "user":
        messages[-1]["content"].append(block)
    else:
        messages.append({"role": "user", "content": [block]})


def _consume_steer(path: Path, consumed_subdir: Path) -> None:
    """Move one drained steer into ``consumed/`` (created if absent)."""
    try:
        state.make_dir(consumed_subdir)
        state.atomic_replace(path, consumed_subdir / path.name, cleanup_source=False)
    except OSError:
        pass


def _steer_abort_point() -> None:
    """Hard-exit between the transcript flush and the rename, when armed (test)."""
    if os.environ.get(STEER_ABORT_VARIABLE, "").strip():
        os._exit(137)


#: Unicode line and paragraph separators, outside `escape_control`'s C0/C1 range;
#: stripped so no separator a renderer honours can forge or split a log line.
_UNICODE_LINE_SEPARATORS = {0x2028: " ", 0x2029: " "}


def _steer_excerpt(text: str) -> str:
    """Render a steer's text for one log line: control-clean and truncated."""
    cleaned = state.escape_control(text.translate(_UNICODE_LINE_SEPARATORS))
    if len(cleaned) <= _STEER_EXCERPT_LIMIT:
        return cleaned
    return cleaned[: _STEER_EXCERPT_LIMIT - 1] + "…"


def _stream_turn(
    client: anthropic.Anthropic,
    profile: Profile,
    spec: JobSpec,
    registry: Registry,
    messages: list[dict],
    config: LoopConfig,
    transcript: Transcript,
    tools: list[dict],
) -> tuple[list[dict], dict | None]:
    """Stream one assistant turn through the supervision ladder.

    The ladder replays a snapshot of ``messages`` on every attempt and returns
    only a turn that reached ``message_stop``, so a retried attempt contributes
    nothing here — and nothing to the canonical history the caller appends to.
    The usage returned is that ``message_stop`` message's, so a retried attempt
    contributes no usage and exactly-once accounting is automatic.

    ``tools`` is the native + Worker-MCP tools array, built ONCE at Job start and
    reused every turn (same object → byte-stable prefix, ADR 0001).
    """
    message = stream_with_ladder(
        client,
        model=profile.model,
        max_tokens=profile.max_tokens,
        system=_system_prompt(spec),
        tools=tools,
        messages=messages,
        request_extras=profile.request_extras,
        config=config,
        on_retry=transcript.append_retry,
    )
    content = [_block_to_dict(block) for block in message.content]
    return content, _usage_to_dict(message)


#: Answers a sibling report_result after the first validating one, so no
#: unanswered tool_use goes into the next request (the provider-400 trap).
_SIBLING_REPORT = (
    "[chinamax] Ignored: this job already terminated on the first report_result of "
    "this turn. Only the first report_result ends the job."
)


def _run_tool_uses(
    tool_uses: list[dict],
    registry: Registry,
    context: ToolContext,
    reporter: Callable[[str, str], None] | None,
    policy: Policy,
    injected: set[str],
    stop_hook_active: bool,
) -> tuple[list[dict], dict | None, bool]:
    """Execute every tool_use block in arrival order, under the Host policy.

    The per-tool-call order is PreToolUse → (deny ends it: error tool_result, no
    dispatch) → dispatch → PostToolUse (successful dispatches only) → lazy Memory
    injection (successful results only). PreToolUse-allow ``additionalContext``
    and PostToolUse context ride as text blocks after that tool's tool_result in
    the same user turn. An advertised MCP name routes to its server BEFORE the
    Registry; every native name still goes through the Registry (unknown names
    already come back as error tool_results). ``report_result`` is the Stop gate.

    Args:
        tool_uses: The turn's tool_use blocks.
        registry: The Job's posture-filtered registry.
        context: The Job's tool context.
        reporter: The progress reporter, called around each execution.
        policy: The Job's Host policy (hooks, Memory, MCP).
        injected: The already-injected Memory-file path set, updated in place.
        stop_hook_active: Whether a Stop hook has fired earlier in this Job.

    Returns:
        The tool_result (and context) blocks to send back, the terminal
        ``report_result`` payload when one was accepted, and whether a Stop hook
        fired this turn.
    """
    results: list[dict] = []
    payload: dict | None = None
    stop_fired = False
    terminal_seen = False
    for block in tool_uses:
        name = block.get("name")
        tool_use_id = block.get("id")
        value = block.get("input")

        if name == REPORT_RESULT:
            problem = registry.validate(name, value)
            if problem is not None:
                # A schema-invalid report_result is answered with an error before
                # the terminal branch and fires no Stop.
                results.append(_error_result(tool_use_id, problem))
                continue
            if terminal_seen:
                # The FIRST validating report_result of a turn is the terminus; a
                # sibling is answered so no unanswered tool_use trips a 400.
                results.append(_error_result(tool_use_id, _SIBLING_REPORT))
                continue
            terminal_seen = True
            decision = policy.stop(stop_hook_active)
            stop_fired = True
            if decision.blocked:
                # A block answers the report_result with the hook reason and
                # continues the loop (never a bare user turn — a 400).
                results.append(_error_result(tool_use_id, decision.reason))
                _report(reporter, PHASE_REPORTING, "[policy] Stop hook blocked report_result")
                continue
            payload = value
            _report(reporter, PHASE_REPORTING, f"report_result: {_preview(value)}")
            continue

        is_mcp = policy.is_mcp_tool(name)
        if is_mcp:
            hook_name, hook_input, kind = name, value if isinstance(value, dict) else {}, "mcp"
            pre = policy.pre_tool_use(hook_name, hook_input)
        elif name == "apply_patch":
            hook_name, hook_input, kind = None, None, "patch"
            patch_text = value.get("patch") if isinstance(value, dict) else None
            pre = policy.pre_tool_use_patch(patch_text)
        else:
            hook_name, hook_input = translate_tool(name, value)
            kind = "native"
            pre = policy.pre_tool_use(hook_name, hook_input)

        if not pre.allowed:
            # PreToolUse deny → tool NOT executed, reason returned as the
            # (error) tool_result so the worker sees actionable feedback.
            _report(reporter, PHASE_RUNNING_TOOL, f"{name}: denied by PreToolUse hook")
            results.append(_error_result(tool_use_id, pre.reason))
            continue

        _report(reporter, PHASE_RUNNING_TOOL, f"{name}: {_preview(value)}")
        if is_mcp:
            content, is_error = policy.dispatch_mcp(name, value)
        else:
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
        # PreToolUse allow-with-additionalContext rides after the tool_result.
        for text in pre.contexts:
            results.append({"type": "text", "text": text})
        if is_error:
            # PostToolUse fires only after a SUCCESSFUL dispatch; a denied or
            # errored call is never a lazy-Memory touch.
            continue

        if kind == "patch":
            post = policy.post_tool_use_patch(patch_text)
        else:
            post = policy.post_tool_use(hook_name, hook_input, content, False)
        for text in post:
            results.append({"type": "text", "text": text})
        # Lazy Memory injection: bash and MCP tool paths are out of scope.
        if not is_mcp:
            for lazy in policy.lazy_blocks_for_path(name, value, injected):
                results.append({"type": "text", "text": lazy})
    return results, payload, stop_fired


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


def _usage_to_dict(message: object) -> dict | None:
    """Return the completed message's provider-reported usage, None values dropped.

    Defensive: an Anthropic-compatible provider is not guaranteed to send usage,
    so an absent attribute yields None rather than a crash. Extra provider fields
    (service_tier etc.) flow through deliberately — mode="json" keeps them
    serializable.
    """
    usage = getattr(message, "usage", None)
    if usage is None:
        return None
    data = usage.model_dump(mode="json")
    return {key: value for key, value in data.items() if value is not None}


def _emit_usage(
    reporter: Callable[[str, str], None] | None, turn_number: int, usage: dict | None
) -> None:
    """Emit one turn's provider-reported usage: stderr AND the Job log.

    stderr reaches the exec path (and the spawn log on a detached worker); the
    reporter mirror is what lands in jobs/<id>.log — the worker's stderr goes to
    the SPAWN log, not the Job log. ``PHASE_CALLING_MODEL`` keeps the record's
    stored phase honest mid-run (``reporting`` would lie to ``status``); the
    sorted keys make ``"event": "usage"`` a stable grep literal in the log.
    """
    if not usage:
        return
    details = {"turn": turn_number, **usage}
    emit_event("usage", details)
    _report(
        reporter,
        PHASE_CALLING_MODEL,
        json.dumps({"event": "usage", **details}, sort_keys=True),
    )


def _append(
    transcript: Transcript, messages: list[dict], role: str, content: list[dict]
) -> None:
    """Record one turn in both the in-memory history and the durable Thread."""
    messages.append({"role": role, "content": content})
    transcript.append_message(role, content)
