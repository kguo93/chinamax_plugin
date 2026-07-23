"""chinamax Runtime — the provider-agnostic agent loop that owns one Job."""


class ChinamaxError(Exception):
    """A Runtime failure meant for the operator, reported at the CLI seam."""


class ToolError(Exception):
    """A tool refusal meant for the worker model, rendered as an error observation.

    Distinct from ChinamaxError: this never reaches the operator and never ends
    the Job. It carries the whole message the model reads, so raise it with text
    a model can act on — a confinement violation, a denylist block, an edit whose
    target was not found.
    """
