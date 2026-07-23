"""chinamax Runtime — the provider-agnostic agent loop that owns one Job."""


class ChinamaxError(Exception):
    """A Runtime failure meant for the operator, reported at the CLI seam."""
