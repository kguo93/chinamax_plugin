"""CLI seam for the Runtime. This slice ships one verb: ``exec``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from chinamax import ChinamaxError, profiles, provider
from chinamax.loop import run_loop
from chinamax.spec import load_spec
from chinamax.transcript import Transcript


def run_exec(spec_path: str | Path) -> int:
    """Run one Job from a job-spec file.

    The shared exec entry: sanitizing the environment lives here, not in the
    argument parsing, so an in-process caller gets it too.

    Args:
        spec_path: Path to the job-spec JSON file.

    Returns:
        0 once the result has been written.

    Raises:
        ChinamaxError: On any validation or configuration failure.
        Exception: Provider errors propagate; this slice runs with no retries.
    """
    provider.sanitize_environment()
    spec = load_spec(spec_path)
    profile = profiles.resolve_profile(spec.profile)
    client = provider.build_client(profile, profiles.resolve_key(profile))
    with Transcript(spec.transcript_path) as transcript:
        payload = run_loop(client, profile, spec, transcript)
    _write_result(spec.result_path, payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the requested verb.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(prog="chinamax", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    exec_parser = subcommands.add_parser("exec", help="run one Job from a job spec")
    exec_parser.add_argument("spec_path", help="path to the job-spec JSON file")
    args = parser.parse_args(argv)

    try:
        return run_exec(args.spec_path)
    except ChinamaxError as error:
        print(f"chinamax: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # provider/tool failures end the Job non-zero
        print(f"chinamax: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


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
