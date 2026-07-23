"""The streaming Messages tool-use loop.

Every ``tool_use`` block in an assistant turn is executed in arrival order and
answered by a ``tool_result`` carrying its ``tool_use_id``; the loop ends only
when ``report_result`` arrives. The transcript is write-ahead: the outgoing
delta is appended and flushed before the API call, the assembled assistant turn
after the stream completes.
"""

from __future__ import annotations

import anthropic

from chinamax.profiles import Profile
from chinamax.spec import JobSpec
from chinamax.tools import (
    BASH,
    REPORT_RESULT,
    TOOLS,
    TOOLS_BY_NAME,
    format_bash_result,
    run_bash,
    validate_input,
)
from chinamax.transcript import Transcript

SYSTEM_TEMPLATE = """You are a worker model executing one task inside the workspace {workspace}.

{posture}

Use the bash tool to inspect and change that workspace; every command runs with \
{workspace} as its working directory.

When the task is finished — whether it succeeded or not — you MUST call the \
report_result tool. That call is the only way to end this job, and its payload is \
relayed back verbatim as the job's result: set outcome to "completed", "blocked" or \
"failed", write a summary, and fill in changed_files, commands_run, tests, failures \
and concerns as far as they apply."""

WRITE_POSTURE = "You may create and modify files in this workspace."
READ_ONLY_POSTURE = (
    "This job is read-only: investigate and report, do not modify this workspace."
)

NUDGE = (
    "Your last turn used no tools. Keep working with the bash tool, or call the "
    "report_result tool to finish this job — report_result is the only way to end it."
)


def run_loop(
    client: anthropic.Anthropic,
    profile: Profile,
    spec: JobSpec,
    transcript: Transcript,
) -> dict:
    """Drive one Job to its report_result.

    Args:
        client: The SDK client built for the Profile.
        profile: The resolved Profile.
        spec: The validated job spec.
        transcript: The Job's open Thread transcript.

    Returns:
        The ``report_result`` payload, verbatim.
    """
    messages: list[dict] = []
    _append(transcript, messages, "user", [{"type": "text", "text": spec.prompt}])
    while True:
        content = _stream_turn(client, profile, spec, messages)
        _append(transcript, messages, "assistant", content)

        tool_uses = [block for block in content if block.get("type") == "tool_use"]
        if not tool_uses:
            # Keys on the absence of tool_use, never on stop_reason: end_turn,
            # max_tokens and stop_sequence all produce a tool-less turn, and
            # re-sending would repeat the same request forever.
            _append(transcript, messages, "user", [{"type": "text", "text": NUDGE}])
            continue

        results, payload = _run_tool_uses(tool_uses, spec)
        if payload is not None:
            # Siblings of the terminal report_result are still answered in the
            # durable record, even though no further request is sent.
            if results:
                _append(transcript, messages, "user", results)
            return payload
        _append(transcript, messages, "user", results)


def _stream_turn(
    client: anthropic.Anthropic,
    profile: Profile,
    spec: JobSpec,
    messages: list[dict],
) -> list[dict]:
    """Stream one assistant turn and return its content blocks as plain dicts."""
    with client.messages.stream(
        model=profile.model,
        max_tokens=profile.max_tokens,
        system=_system_prompt(spec),
        tools=TOOLS,
        messages=messages,
    ) as stream:
        message = stream.get_final_message()
    return [_block_to_dict(block) for block in message.content]


def _run_tool_uses(tool_uses: list[dict], spec: JobSpec) -> tuple[list[dict], dict | None]:
    """Execute every tool_use block in arrival order.

    Args:
        tool_uses: The turn's tool_use blocks.
        spec: The validated job spec.

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

        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            results.append(
                _error_result(
                    tool_use_id,
                    f"unknown tool {name!r}; available tools: "
                    f"{', '.join(TOOLS_BY_NAME)}",
                )
            )
            continue
        problem = validate_input(tool["input_schema"], value)
        if problem is not None:
            results.append(_error_result(tool_use_id, f"invalid input for {name!r}: {problem}"))
            continue

        if name == REPORT_RESULT:
            # The terminal block is deliberately left unanswered: it IS the terminus.
            if payload is None:
                payload = value
        elif name == BASH:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": format_bash_result(run_bash(value["command"], spec.workspace)),
                }
            )
    return results, payload


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
