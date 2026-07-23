"""The report_result tool: the Job's only terminal path.

Its input becomes the Job's structured result verbatim — the Runtime never
normalizes, synthesizes or audits the fields (ADR 0007).
"""

from __future__ import annotations

REPORT_RESULT = "report_result"

_STRING_LIST = {"type": "array", "items": {"type": "string"}}

REPORT_RESULT_TOOL = {
    "name": REPORT_RESULT,
    "description": (
        "Finish this job and report its outcome. Calling this tool is the only way "
        "to end the job; the payload is relayed back verbatim as the job's result."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["completed", "blocked", "failed"]},
            "summary": {"type": "string"},
            "changed_files": _STRING_LIST,
            "commands_run": _STRING_LIST,
            "tests": _STRING_LIST,
            "failures": _STRING_LIST,
            "concerns": _STRING_LIST,
        },
        "required": ["outcome", "summary"],
    },
}
