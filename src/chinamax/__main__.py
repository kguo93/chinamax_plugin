"""CLI seam for the Runtime. This slice ships one verb: ``exec``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from chinamax import ChinamaxError, profiles, provider
from chinamax.liveness import LoopConfig, RunFailure, build_config, emit_event
from chinamax.loop import run_loop
from chinamax.spec import load_spec
from chinamax.transcript import Transcript


def run_exec(spec_path: str | Path, config: LoopConfig | None = None) -> int:
    """Run one Job from a job-spec file.

    The shared exec entry: sanitizing the environment lives here, not in the
    argument parsing, so an in-process caller gets it too. It is also the
    failure seam — the Runtime owns no state store, so ladder exhaustion and a
    permanent provider error end the run here, as a nonzero exit plus one
    structured JSON line through the progress reporter.

    Args:
        spec_path: Path to the job-spec JSON file.
        config: Supervision configuration to override the defaults with; the
            spec's own overrides are applied on top. In-process callers supply
            one to inject the clock, sleeper and jitter seams.

    Returns:
        0 once the result has been written, 1 on a terminal provider failure.

    Raises:
        ChinamaxError: On any validation or configuration failure.
    """
    provider.sanitize_environment()
    spec = load_spec(spec_path)
    config = build_config(spec, config)
    profile = profiles.resolve_profile(spec.profile)
    client = provider.build_client(
        profile, profiles.resolve_key(profile), config.inactivity_timeout_s
    )
    try:
        with Transcript(spec.transcript_path, clock=config.clock) as transcript:
            payload = run_loop(client, profile, spec, transcript, config)
    except RunFailure as failure:
        emit_event("failure", failure.payload)
        return 1
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
