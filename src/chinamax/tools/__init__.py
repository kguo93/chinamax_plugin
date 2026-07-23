"""The Runtime's tool registry. This slice advertises bash and report_result."""

from __future__ import annotations

from chinamax.tools.bash import BASH_TOOL, format_bash_result, run_bash
from chinamax.tools.report_result import REPORT_RESULT, REPORT_RESULT_TOOL

BASH = BASH_TOOL["name"]
TOOLS = [BASH_TOOL, REPORT_RESULT_TOOL]
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}

__all__ = [
    "BASH",
    "BASH_TOOL",
    "REPORT_RESULT",
    "REPORT_RESULT_TOOL",
    "TOOLS",
    "TOOLS_BY_NAME",
    "format_bash_result",
    "run_bash",
    "validate_input",
]


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
    elif kind == "array":
        if not isinstance(item, list):
            return f"field {field!r} must be an array"
        if declared.get("items", {}).get("type") == "string" and not all(
            isinstance(element, str) for element in item
        ):
            return f"field {field!r} must be an array of strings"
    return None
