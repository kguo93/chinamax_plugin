"""The report_result tool: the Job's only terminal path.

Its input becomes the Job's result verbatim — the Runtime never normalizes,
synthesizes or audits it (ADR 0007).
"""

from __future__ import annotations

REPORT_RESULT = "report_result"

REPORT_RESULT_TOOL = {
    "name": REPORT_RESULT,
    "description": (
        "Finish this job and deliver your final answer. Calling this tool is the "
        "only way to end the job; response is relayed back verbatim as the job's "
        "result."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "response": {
                "type": "string",
                "description": (
                    "Your complete final answer, in full — everything the person "
                    "who dispatched this job should read. This is your final "
                    "message to them, not a summary of it."
                ),
            },
        },
        "required": ["response"],
    },
}


class ReportResult:
    """The terminal tool: advertised like any other, but deliberately not executable.

    It carries no ``execute``, because there is nothing to execute — the loop
    recognizes the name, takes the payload as the Job's result and stops. Giving
    it an executor would imply the Runtime does something to the payload, and
    ADR 0007 says it does not.
    """

    spec = REPORT_RESULT_TOOL
    writes = False
