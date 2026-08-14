"""The seven command files map onto real seam verbs, and the launcher normalizer.

`test_each_command_maps_verb` is a behavioral check on the SEAM, not a substring
match: the `!` line extracted from each doc is run through the real
`normalize_argv` and the real argument parser, so a command that named a verb the
parser rejects — or forwarded flags it does not accept — fails here.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from chinamax.__main__ import build_parser, main, normalize_argv
from conftest import SYNTHETIC_KEYS, write_overlay

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = REPO_ROOT / "commands"
LAUNCHER = REPO_ROOT / "scripts" / "chinamax"
BANG_LINE = re.compile(r"^!`(.+)`\s*$", re.MULTILINE)

#: Unique marker the fake python3 prints once the shim reaches its exec.
PY_MARKER = "CHINAMAX_FAKE_PY_REACHED"

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
def test_each_command_maps_verb(verb, capsys, keyless_home):
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
        assert main(["--host", "claude", "profiles"]) == 0
        out = capsys.readouterr().out
        assert "PRESENT" in out or "MISSING" in out
        for value in SYNTHETIC_KEYS.values():
            assert value not in out, "profiles leaked an API key value"
        # The shipped deepseek row shows its resolved extras, compact and sorted.
        deepseek_row = next(line for line in out.splitlines() if line.startswith("deepseek"))
        assert 'extras={"extra_body":{"reasoning":{"effort":"max"}}}' in deepseek_row

        # An overlay that empties the extras drops the suffix (empty ⇒ omitted).
        write_overlay(keyless_home, [{"name": "deepseek", "request_extras": {}}])
        assert main(["--host", "claude", "profiles"]) == 0
        deepseek_row = next(
            line for line in capsys.readouterr().out.splitlines() if line.startswith("deepseek")
        )
        assert "extras=" not in deepseek_row


def test_setup_command_approve_protocol():
    """commands/setup.md carries the prerequisite approve-and-install protocol."""
    text = (COMMANDS_DIR / "setup.md").read_text(encoding="utf-8")
    assert '"approve"' in text  # the exact consent keyword
    assert "run_policy" in text  # dispatch by run_policy
    assert "stop-on-first-failure" in text.lower()
    # Exactly one Windows zero-state fallback block (the one accepted duplication).
    assert text.count("winget install --id Git.Git") == 1
    # The dropped installer-cleanup lines never appear (operator override).
    assert 'rm "$HOME/.chinamax-miniconda.sh"' not in text
    assert 'del "%TEMP%\\chinamax-miniconda.exe"' not in text


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


def _write_fake(path: Path, body: str) -> None:
    """Write a `#!/bin/sh` fake executable on PATH and mark it runnable."""
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def _macos_no_python_env(tmp_path: Path, *, real_python: bool) -> dict[str, str]:
    """Craft a hermetic macOS-simulated env where the launcher reaches its guard.

    Fakes `uname`→Darwin, `xcode-select`→exit 1, and `conda`→exit 1 on PATH, with
    an empty HOME (no ~/miniconda3) and an empty CLAUDE_PLUGIN_DATA (no recorded
    python-path), so every interpreter rung before the macOS guard is neutralized
    and PATH's only resolvable python3 is /usr/bin/python3. When `real_python` is
    set, a fake `python3` printing PY_MARKER lands FIRST on PATH, so
    `command -v python3` resolves off /usr/bin/python3 and the guard is passed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake(bin_dir / "uname", "#!/bin/sh\nprintf 'Darwin\\n'\n")
    _write_fake(bin_dir / "xcode-select", "#!/bin/sh\nexit 1\n")
    _write_fake(bin_dir / "conda", "#!/bin/sh\nexit 1\n")
    if real_python:
        _write_fake(
            bin_dir / "python3",
            f"#!/bin/sh\nprintf '{PY_MARKER} %s\\n' \"$*\"\nexit 0\n",
        )
    home = tmp_path / "home"
    home.mkdir()
    plugin_data = tmp_path / "plugindata"
    plugin_data.mkdir()
    # A minimal env: CHINAMAX_PYTHON is absent (unset), only the fixture bin and
    # /usr/bin:/bin are on PATH — never conda's python.
    return {
        "HOME": str(home),
        "CLAUDE_PLUGIN_DATA": str(plugin_data),
        "CHINAMAX_HOST": "claude",
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
    }


def test_macos_shim_stops_without_real_python(tmp_path):
    """On macOS with no real python3 (only the /usr/bin/python3 CLT stub) the
    shim refuses to exec, stops with install guidance, and the doctor never runs."""
    env = _macos_no_python_env(tmp_path, real_python=False)
    result = subprocess.run(
        ["bash", str(LAUNCHER), "setup"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "no usable Python 3" in result.stderr
    assert "install a real python 3" in result.stderr.lower()
    # The guard emits only to stderr and never reaches the exec, so the doctor
    # produced no report and its unique marker path was never taken.
    assert result.stdout == ""
    assert PY_MARKER not in result.stdout


def test_macos_shim_runs_with_real_python(tmp_path):
    """A non-stub python3 (resolving off /usr/bin/python3) passes the guard, so the
    shim reaches its `python3 -m chinamax setup` exec and prints no guidance."""
    env = _macos_no_python_env(tmp_path, real_python=True)
    result = subprocess.run(
        ["bash", str(LAUNCHER), "setup"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert PY_MARKER in result.stdout
    assert "-m chinamax setup" in result.stdout  # the module and forwarded verb
    assert "no usable Python 3" not in result.stderr
