"""The seven command files map onto real seam verbs, and the launcher normalizer.

`test_each_command_maps_verb` is a behavioral check on the SEAM, not a substring
match: the `!` line extracted from each doc is run through the real
`normalize_argv` and the real argument parser, so a command that named a verb the
parser rejects — or forwarded flags it does not accept — fails here.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from chinamax.__main__ import build_parser, main, normalize_argv
from conftest import SYNTHETIC_KEYS

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = REPO_ROOT / "commands"
BANG_LINE = re.compile(r"^!`(.+)`\s*$", re.MULTILINE)

#: verb -> a representative raw "$ARGUMENTS" string the parser must accept once
#: normalized. Only the FOUR surviving command files map to a seam verb here
#: (task uses the Agent tool, tested separately); the internal seam verbs
#: result/cancel/resume/steer/logs still exist in the CLI but have no command file
#: (2026-07-30), so they are exercised by `test_argument_normalization` directly.
SAMPLE_ARGS = {
    "status": "task-abc-000001 --wait --timeout-ms 5000",
    "profiles": "",
    "setup": "--json",
}


def _bang_tokens(verb: str) -> list[str]:
    """Return the shell tokens of a command file's single `!` launcher line."""
    text = (COMMANDS_DIR / f"{verb}.md").read_text(encoding="utf-8")
    matches = BANG_LINE.findall(text)
    assert len(matches) == 1, f"{verb}.md must carry exactly one `!` line, got {matches}"
    return shlex.split(matches[0])


@pytest.mark.parametrize("verb", sorted(SAMPLE_ARGS))
def test_each_command_maps_verb(verb, capsys):
    """Each command invokes its verb through the launcher, quoting "$ARGUMENTS",
    with flags the CLI's own parser accepts (and profiles never leaks a key)."""
    tokens = _bang_tokens(verb)

    # The launcher, the verb, and the single quoted "$ARGUMENTS" element.
    assert tokens[0].endswith("scripts/chinamax"), tokens
    assert tokens[1] == verb, tokens
    assert tokens[-1] == "$ARGUMENTS", tokens
    assert '"$ARGUMENTS"' in (COMMANDS_DIR / f"{verb}.md").read_text(encoding="utf-8")

    # The flags a command file forwards reach argparse as real tokens and parse.
    normalized = normalize_argv([verb, SAMPLE_ARGS[verb]])
    parsed = build_parser().parse_args(normalized)
    assert parsed.command == verb

    # profiles renders PRESENT/MISSING and NEVER a key value.
    if verb == "profiles":
        assert main(["profiles"]) == 0
        out = capsys.readouterr().out
        assert "PRESENT" in out or "MISSING" in out
        for value in SYNTHETIC_KEYS.values():
            assert value not in out, "profiles leaked an API key value"


def test_argument_normalization():
    """The launcher normalizer collapses the single quoted "$ARGUMENTS" element."""
    # Blank / whitespace-only -> no arguments at all (not a stray "").
    assert normalize_argv(["status", ""]) == ["status"]
    assert normalize_argv(["status", "   "]) == ["status"]

    # A lone raw string is split so its flags reach the parser as tokens.
    assert normalize_argv(["status", "abc --wait"]) == ["status", "abc", "--wait"]
    assert normalize_argv(["logs", "task-x --tail 5"])[1:] == ["task-x", "--tail", "5"]

    # An already-split argv (tests, the Bridge, dispatch) is inert; internal verbs
    # are never touched even with a single tail element (a spec path with a space).
    assert normalize_argv(["exec", "/tmp/a b/spec.json"]) == ["exec", "/tmp/a b/spec.json"]
    assert normalize_argv(["status", "task-x", "--wait"]) == ["status", "task-x", "--wait"]

    # `steer` mirrors resume's transport: a leading `task-` selector is peeled and
    # the message is kept intact after `--`; a bare steer keeps the whole message.
    assert normalize_argv(["steer", "task-abc-000001 stop touching X"]) == [
        "steer", "task-abc-000001", "--", "stop touching X",
    ]
    assert normalize_argv(["steer", "stop touching X"]) == ["steer", "--", "stop touching X"]
    # A blank steer collapses to no args (the bare active Job, message via stdin).
    assert normalize_argv(["steer", "  "]) == ["steer"]

    # A resume prompt with spaces, quotes, a leading dash, and a newline survives
    # byte-identical through the `--`/stdin path.
    prompt = '-x say "hi there"\nand a second line'
    normalized = normalize_argv(["resume", prompt])
    assert normalized[1] == "--"
    assert normalized[-1] == prompt

    parsed = build_parser().parse_args(normalized)
    from chinamax.__main__ import DEFAULT_RESUME_PROMPT, _read_prompt, _split_resume_args

    selector, words = _split_resume_args(parsed.args)
    assert selector is None
    assert _read_prompt(words, default=DEFAULT_RESUME_PROMPT) == prompt

    # A leading task- selector is peeled; the follow-up stays intact.
    with_id = normalize_argv(["resume", "task-abc-000001 keep going"])
    parsed_id = build_parser().parse_args(with_id)
    selector_id, words_id = _split_resume_args(parsed_id.args)
    assert selector_id == "task-abc-000001"
    assert _read_prompt(words_id, default=DEFAULT_RESUME_PROMPT) == "keep going"
