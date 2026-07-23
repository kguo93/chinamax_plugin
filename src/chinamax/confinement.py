r"""Tool-layer confinement: workspace containment and bash command policy (ADR 0005).

Two independent controls live here because they answer the same question — "may
this tool_use touch that?" — for the two shapes a tool argument takes:

* `resolve_in_workspace` turns a model-supplied path into a real path proven to
  be inside the workspace, component-wise, with symlinks followed to their
  target. It is the only way a file tool may turn a string into a path.
* `lex_command` turns a bash command line into pipeline stages with their
  command tokens identified. `denied_reason` (the operator's hard bans) and
  `write_shaped_reason` (read-only Jobs) both read that single lexer's output,
  so the two policies cannot drift apart over what a command line actually says.

Matching is on COMMAND POSITION, never on raw substrings: that is what lets
`grep -rn "git push" docs/` and `pytest -k test_shutdown` run, because the
quoted string lexes to one non-command token.

Deliberately NOT defended, so this coverage is not mistaken for comprehensiveness:
network egress (`curl`/`wget`/`nc` to arbitrary hosts), process signalling
(`kill`/`pkill`), and quoting or substitution evasions (`\rm`, `$(…)`, `${IFS}`).
bash is cwd-pinned but not path-confined — absolute paths and `..` stay reachable
from it, and a read-only Job can still write through `python -c`, a heredoc or
`truncate`. ADR 0005 documents that residual risk rather than defending it.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from chinamax import ToolError

#: Shell separators that end one pipeline stage and begin the next.
SEPARATORS = frozenset({"|", "||", ";", "&&", "&"})

#: Command tokens that wrap another command rather than being one.
_WRAPPERS = frozenset({"env", "command"})

#: Grouping tokens a stage can open with.
_GROUPS = frozenset({"(", "((", "{"})

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Denied command tokens mapped to the reason shown to the model.
_DENIED_COMMANDS = {
    "rm": "deletes files",
    "rmdir": "deletes directories",
    "unlink": "deletes files",
    "shred": "destroys file contents",
    "dd": "writes raw blocks over devices and files",
    "wipefs": "erases filesystem signatures",
    "fdisk": "edits partition tables",
    "parted": "edits partition tables",
    "sudo": "escalates privileges out of the workspace",
    "shutdown": "controls system power",
    "reboot": "controls system power",
    "poweroff": "controls system power",
}

#: Interpreters that must never be the receiving end of a pipe.
_PIPE_TARGETS = frozenset({"sh", "bash", "zsh", "python", "python3"})

#: git global options that consume the following token, so the subcommand scan
#: does not mistake their value for the subcommand.
_GIT_VALUE_FLAGS = frozenset(
    {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)

_REDIRECT_CHARACTERS = frozenset("<>&")


@dataclass(frozen=True)
class ToolContext:
    """What every tool needs to know about the Job it is executing inside."""

    #: The workspace realpath, resolved once per Job.
    root: Path
    #: False for a read-only Job: write-class tools are gone and bash is filtered.
    write: bool
    #: Per-command bash timeout in seconds.
    bash_timeout_s: float


@dataclass(frozen=True)
class Stage:
    """One pipeline stage of a command line."""

    #: The separator that introduced this stage; None for the first.
    separator: str | None
    #: Every token in the stage, redirections included.
    tokens: tuple[str, ...]
    #: The command token, path components and wrapper prefixes stripped.
    command: str | None
    #: The tokens after the command token.
    args: tuple[str, ...]


def resolve_in_workspace(workspace: Path, path: str) -> Path:
    """Resolve a model-supplied path and prove it lies inside the workspace.

    ``~`` is expanded and a relative path is taken against the workspace, then
    the result is realpathed — so symlinks are followed and it is the TARGET
    that must be inside. Containment is compared component-wise, never by string
    prefix: workspace ``/tmp/ws`` rejects ``/tmp/ws-evil/f``.

    Args:
        workspace: The workspace realpath, resolved once per Job.
        path: The path the model supplied.

    Returns:
        The realpath, which may not exist yet (write_file creates it).

    Raises:
        ToolError: If the path escapes the workspace, or is not a string.
    """
    if not isinstance(path, str) or not path:
        raise ToolError("path must be a non-empty string")
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(workspace, expanded)
    resolved = Path(os.path.realpath(expanded))
    if not resolved.is_relative_to(workspace):
        raise ToolError(
            f"path {path!r} resolves to {resolved}, which is outside the workspace "
            f"{workspace}; this job may only touch files inside the workspace"
        )
    return resolved


def contained(workspace: Path, path: str | os.PathLike[str]) -> Path | None:
    """Return the realpath of a path already found on disk, or None if it escapes.

    The traversal counterpart of `resolve_in_workspace`: directory walks
    re-validate every entry they discover, because an entry can itself be a
    symlink pointing out of the workspace. Escapes are skipped rather than
    raised, so one hostile symlink does not abort an otherwise fine search.

    Args:
        workspace: The workspace realpath.
        path: A path produced by a directory walk.

    Returns:
        The realpath when it is inside the workspace, otherwise None.
    """
    resolved = Path(os.path.realpath(path))
    return resolved if resolved.is_relative_to(workspace) else None


def lex_command(command: str) -> list[Stage]:
    """Split a command line into pipeline stages with their commands identified.

    Args:
        command: The command line as the model wrote it.

    Returns:
        The stages in order.

    Raises:
        ToolError: If the line cannot be lexed — a line the policy cannot read
            is a line it cannot vouch for, and bash would reject it too.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # bash only starts a comment at a word boundary. Leaving shlex's default
    # commenters in place would swallow the rest of `echo a#b; rm -rf x` and
    # hide the `rm` stage from the denylist entirely.
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise ToolError(
            f"could not parse the command ({exc}); rewrite it without unbalanced quotes"
        ) from exc

    stages: list[Stage] = []
    separator: str | None = None
    current: list[str] = []
    for token in tokens:
        if token in SEPARATORS:
            stages.append(_build_stage(separator, current))
            separator = token
            current = []
        else:
            current.append(token)
    stages.append(_build_stage(separator, current))
    return stages


def denied_reason(command: str) -> str | None:
    """Return why the operator's denylist refuses this command, or None.

    A pure function over the shared lexer, callable on its own as well as
    through the bash tool.

    Args:
        command: The command line as the model wrote it.

    Returns:
        The observation text explaining the block, or None when it may run.

    Raises:
        ToolError: If the command line cannot be lexed.
    """
    for stage in lex_command(command):
        reason = _stage_denied(stage)
        if reason is not None:
            return (
                f"blocked by the operator denylist: {reason}. The command was not "
                "executed — find another way to make progress."
            )
    return None


def write_shaped_reason(command: str) -> str | None:
    """Return why a read-only Job refuses this command, or None.

    A conservative family: redirection onto a real file, `tee`, `mv`/`cp`,
    `sed -i`, `git commit`/`git apply`, and `pip install`. Redirections are
    judged by their TARGET, so `2>&1`, `>&2` and `>/dev/null` stay allowed —
    `grep x f 2>&1 | head` is an ordinary read.

    Args:
        command: The command line as the model wrote it.

    Returns:
        The observation text explaining the block, or None when it may run.

    Raises:
        ToolError: If the command line cannot be lexed.
    """
    for stage in lex_command(command):
        reason = _stage_write_shaped(stage)
        if reason is not None:
            return (
                f"this job is read-only: {reason}. The command was not executed — "
                "investigate and report instead of modifying the workspace."
            )
    return None


def _build_stage(separator: str | None, tokens: list[str]) -> Stage:
    """Identify one stage's command token among its tokens."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _GROUPS or token in _WRAPPERS or _ASSIGNMENT.match(token):
            index += 1
            continue
        break
    if index >= len(tokens):
        return Stage(separator, tuple(tokens), None, ())
    return Stage(
        separator,
        tuple(tokens),
        os.path.basename(tokens[index]),
        tuple(tokens[index + 1 :]),
    )


def _stage_denied(stage: Stage) -> str | None:
    """Return why one stage is denied, or None."""
    name = stage.command
    if name is None:
        return None
    if stage.separator == "|" and name in _PIPE_TARGETS:
        return f"piping into {name!r} executes fetched code"
    if name in _DENIED_COMMANDS:
        return f"{name!r} {_DENIED_COMMANDS[name]}"
    if name == "mkfs" or name.startswith("mkfs."):
        return f"{name!r} creates a filesystem over a device"
    if name == "git":
        return _git_denied(stage.args)
    return None


def _git_denied(args: tuple[str, ...]) -> str | None:
    """Return why a git invocation is denied, or None."""
    subcommand, rest = _git_subcommand(args)
    if subcommand == "push":
        return "'git push' publishes to a remote"
    if subcommand == "reset" and "--hard" in rest:
        return "'git reset --hard' discards working-tree changes"
    if subcommand == "clean" and _has_force_flag(rest):
        return "'git clean -f' deletes untracked files"
    if subcommand in {"checkout", "switch"} and _has_force_flag(rest):
        return f"'git {subcommand}' with --force discards working-tree changes"
    return None


def _has_force_flag(args: tuple[str, ...]) -> bool:
    """Report whether a force flag is present, clustered spellings included.

    A `\\b`-anchored `-f` pattern would match `-f` but miss `-fd` and `-fdx`,
    so short flags are inspected as clusters of letters.
    """
    for arg in args:
        if arg == "--force":
            return True
        if arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]:
            return True
    return False


def _git_subcommand(args: tuple[str, ...]) -> tuple[str | None, tuple[str, ...]]:
    """Return a git invocation's subcommand and the tokens after it."""
    index = 0
    while index < len(args):
        token = args[index]
        if token in _GIT_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, args[index + 1 :]
    return None, ()


def _stage_write_shaped(stage: Stage) -> str | None:
    """Return why one stage is write-shaped, or None."""
    reason = _redirect_reason(stage.tokens)
    if reason is not None:
        return reason
    name = stage.command
    if name is None:
        return None
    if name == "tee":
        return "'tee' writes its input to a file"
    if name in {"mv", "cp"}:
        return f"{name!r} creates or moves files, in either direction"
    if name == "sed" and any(
        arg.startswith("-i") or arg == "--in-place" for arg in stage.args
    ):
        return "'sed -i' edits files in place"
    if name == "git":
        subcommand, _ = _git_subcommand(stage.args)
        if subcommand in {"commit", "apply"}:
            return f"'git {subcommand}' changes the repository"
    if name in {"pip", "pip3"} and "install" in stage.args:
        return "'pip install' modifies the environment"
    return None


def _redirect_reason(tokens: tuple[str, ...]) -> str | None:
    """Return why a stage's redirections write to a file, or None.

    The operator is the TARGET's, not its own: `>` alone says nothing, because
    `2>&1` and `>/dev/null` are ordinary reads.
    """
    for index, token in enumerate(tokens):
        if ">" not in token or not set(token) <= _REDIRECT_CHARACTERS:
            continue
        target = tokens[index + 1] if index + 1 < len(tokens) else None
        if token.endswith("&") and target is not None and target.isdigit():
            continue
        if target == "/dev/null":
            continue
        return f"the redirection {token!r} writes to {target!r}"
    return None
