"""The Runtime's tool registry and its single dispatch boundary.

One posture-filtered `Registry` supplies BOTH the advertised schema and the
executable dispatch table. That is the enforcement point for read-only Jobs:
omitting write_file from the schema only stops a model that reads the schema,
whereas a `write_file` tool_use replayed from a resumed Thread reaches dispatch
and must be refused there too.

The boundary also owns what every tool would otherwise repeat: input validation,
output truncation, and exception normalization. It catches `Exception` — an
undecodable file, a permission error, a model-supplied regex that does not
compile — and renders each as an error observation, so no tool input can kill a
Job. It deliberately does NOT catch `BaseException`: `KeyboardInterrupt` and
`SystemExit` belong to the jobs scope's cancel path. An unexpected internal
failure is logged with its traceback and returned sanitized, so a Runtime bug is
never laundered into a model-facing "you did something wrong".
"""

from __future__ import annotations

import logging

from chinamax import ToolError
from chinamax.confinement import ToolContext
from chinamax.tools.bash import BashTool, truncate_tail
from chinamax.tools.files import ListDir, ReadFile, StrReplaceEdit, WriteFile
from chinamax.tools.patch import ApplyPatch
from chinamax.tools.report_result import REPORT_RESULT, ReportResult
from chinamax.tools.search import Glob, Grep

#: Every tool, in the order they are advertised.
ALL_TOOLS = (
    BashTool(),
    ReadFile(),
    WriteFile(),
    StrReplaceEdit(),
    ListDir(),
    Grep(),
    Glob(),
    ApplyPatch(),
    ReportResult(),
)

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "ALL_TOOLS",
    "REPORT_RESULT",
    "Registry",
    "build_registry",
    "validate_input",
]


class Registry:
    """The tool set one Job runs with: advertised schemas and dispatch in one place."""

    def __init__(self, tools: tuple[object, ...]) -> None:
        self._tools = {tool.spec["name"]: tool for tool in tools}

    @property
    def schemas(self) -> list[dict]:
        """Return the tool schemas advertised to the provider, in order."""
        return [tool.spec for tool in self._tools.values()]

    @property
    def names(self) -> list[str]:
        """Return the advertised tool names, in order."""
        return list(self._tools)

    def validate(self, name: str, value: object) -> str | None:
        """Check a tool_use against the registry and its schema.

        Args:
            name: The tool name the model used.
            value: The input the model supplied.

        Returns:
            The observation text describing the first problem, or None when the
            call may proceed. A name this Job does not carry — filtered out by
            the read-only posture, or never registered — is a problem here.
        """
        tool = self._tools.get(name)
        if tool is None:
            return (
                f"unknown tool {name!r}; the tools available to this job are: "
                f"{', '.join(self._tools)}"
            )
        problem = validate_input(tool.spec["input_schema"], value)
        if problem is not None:
            return f"invalid input for {name!r}: {problem}"
        return None

    def dispatch(self, name: str, value: object, context: ToolContext) -> tuple[str, bool]:
        """Execute one tool_use and render its observation.

        Args:
            name: The tool name the model used.
            value: The input the model supplied.
            context: The Job's tool context.

        Returns:
            ``(observation, is_error)``. This never raises: every failure mode
            is an observation the loop can hand back and carry on from.
        """
        problem = self.validate(name, value)
        if problem is not None:
            return problem, True
        try:
            return truncate_tail(self._tools[name].execute(value, context)), False
        except ToolError as error:
            return str(error), True
        except Exception as error:  # noqa: BLE001 - no tool input may end the Job
            # Reported by type and message, never as a traceback and never as
            # "you did something wrong": this branch catches the model's bad
            # regex and the Runtime's own bugs alike, and cannot tell them apart.
            _LOGGER.exception("tool %r failed", name)
            return f"the {name!r} tool failed: {type(error).__name__}: {error}", True


def build_registry(write: bool) -> Registry:
    """Build the registry for one Job's posture.

    Args:
        write: False for a read-only Job, which drops every write-class tool
            from both the advertised schema and the dispatch table.

    Returns:
        The Job's registry.
    """
    return Registry(tuple(tool for tool in ALL_TOOLS if write or not tool.writes))


def validate_input(schema: dict, value: object) -> str | None:
    """Check a tool_use input against its declared ``input_schema``.

    Undeclared fields are accepted: the schemas do not set
    ``additionalProperties: false``, and rejecting extras would let a worker's
    embellished report_result payload block the only terminal path.

    Args:
        schema: The tool's ``input_schema``.
        value: The input the model supplied.

    Returns:
        A description of the first problem found, or None when the input is valid.
    """
    if not isinstance(value, dict):
        return "input must be a JSON object"
    properties = schema.get("properties", {})
    for field in schema.get("required", []):
        if field not in value:
            return f"missing required field {field!r}"
    for field, item in value.items():
        problem = _check_field(field, properties.get(field), item)
        if problem is not None:
            return problem
    return None


def _check_field(field: str, declared: dict | None, item: object) -> str | None:
    """Check one field against its declared property schema."""
    if declared is None:
        return None
    kind = declared.get("type")
    if kind == "string":
        if not isinstance(item, str):
            return f"field {field!r} must be a string"
        allowed = declared.get("enum")
        if allowed is not None and item not in allowed:
            return f"field {field!r} must be one of: {', '.join(allowed)}"
    elif kind == "integer":
        # bool subclasses int, so a bare true would otherwise pass as 1.
        if isinstance(item, bool) or not isinstance(item, int):
            return f"field {field!r} must be an integer"
    elif kind == "boolean":
        if not isinstance(item, bool):
            return f"field {field!r} must be a boolean"
    elif kind == "array":
        if not isinstance(item, list):
            return f"field {field!r} must be an array"
        if declared.get("items", {}).get("type") == "string" and not all(
            isinstance(element, str) for element in item
        ):
            return f"field {field!r} must be an array of strings"
    return None
