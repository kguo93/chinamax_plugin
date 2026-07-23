"""The search tools: grep and glob, both walking the workspace in Python.

Traversal goes through `os.walk(followlinks=False)`, never `Path.rglob` or
`glob.glob(recursive=True)`: on the pinned Python 3.12 those FOLLOW directory
symlinks (`recurse_symlinks` only arrives in 3.13), so a symlink to `/` inside
the workspace would quietly hand the model the whole filesystem. Every entry the
walk discovers is re-validated through `contained` before it is opened or
reported, because a symlink out can also appear as a plain file entry.

grep caps both the files it scans and the matches it returns, and says so in the
observation: ADR 0002 gives the loop no wall-clock rescue, so a recursive search
over a vendored tree must bound itself.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path

from chinamax import ToolError
from chinamax.confinement import ToolContext, contained, resolve_in_workspace

MAX_GREP_FILES = 5_000
MAX_GREP_MATCHES = 200

GREP_TOOL = {
    "name": "grep",
    "description": (
        "Search the workspace recursively for a regular expression, returning "
        "'path:line:text' hits. Set 'fixed' to true to search for the pattern "
        "literally, 'path' to search one subtree, and 'include' to filter file "
        "names by a glob such as '*.py'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "fixed": {"type": "boolean"},
            "include": {"type": "string"},
        },
        "required": ["pattern"],
    },
}

GLOB_TOOL = {
    "name": "glob",
    "description": (
        "List workspace files whose path matches a glob pattern, such as '*.py', "
        "'**/*.md' or 'src/*.json'. A pattern without a '/' matches at any depth. "
        "Note that a '**' in the middle of a pattern requires at least one "
        "directory there: 'src/**/*.py' finds 'src/pkg/a.py' but NOT 'src/a.py'. "
        "Use the bare '*.py' form to search every depth. Set 'path' to search one "
        "subtree."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
        "required": ["pattern"],
    },
}


class Grep:
    """Search the workspace for a pattern, bounded in files scanned and hits."""

    spec = GREP_TOOL
    writes = False

    def execute(self, value: dict, context: ToolContext) -> str:
        """Walk the subtree and collect matching lines.

        Raises:
            re.error: On a model-supplied pattern that is not a valid regex; the
                registry renders it as an error observation.
        """
        pattern = value["pattern"]
        matcher = re.compile(re.escape(pattern) if value.get("fixed") else pattern)
        include = value.get("include")
        start = resolve_in_workspace(context.root, value.get("path") or ".")
        if not start.is_dir():
            raise ToolError(f"{value.get('path') or '.'} is not a directory")

        hits: list[str] = []
        scanned = 0
        capped_files = False
        for resolved, relative in walk_files(context.root, start):
            if include is not None and not fnmatch.fnmatch(os.path.basename(relative), include):
                continue
            if scanned >= MAX_GREP_FILES:
                capped_files = True
                break
            scanned += 1
            if _collect_hits(resolved, relative, matcher, hits):
                break

        report = "\n".join(hits) if hits else f"no matches for {pattern!r} in {scanned} file(s)"
        notes = []
        if len(hits) >= MAX_GREP_MATCHES:
            notes.append(f"stopped at the first {MAX_GREP_MATCHES} matches")
        if capped_files:
            notes.append(f"stopped after scanning {MAX_GREP_FILES} files")
        if notes:
            report += f"\n[{'; '.join(notes)} — narrow the search with 'path' or 'include']"
        return report


class Glob:
    """List workspace files whose path matches a glob pattern."""

    spec = GLOB_TOOL
    writes = False

    def execute(self, value: dict, context: ToolContext) -> str:
        """Walk the subtree and collect matching relative paths."""
        pattern = value["pattern"]
        start = resolve_in_workspace(context.root, value.get("path") or ".")
        if not start.is_dir():
            raise ToolError(f"{value.get('path') or '.'} is not a directory")
        matches = [
            relative
            for _, relative in walk_files(context.root, start)
            if matches_glob(relative, pattern)
        ]
        if not matches:
            return f"no files match {pattern!r}"
        return "\n".join(matches)


def walk_files(root: Path, start: Path) -> Iterator[tuple[Path, str]]:
    """Yield every confined file under a subtree, in a stable order.

    Args:
        root: The workspace realpath.
        start: The confined subtree to walk.

    Yields:
        ``(realpath, workspace-relative posix path)``. The relative path names
        the entry as the walk found it — the name the model can pass back —
        while the realpath is what the caller may open.
    """
    for directory, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if contained(root, os.path.join(directory, name)) is not None
        )
        for name in sorted(filenames):
            entry = os.path.join(directory, name)
            resolved = contained(root, entry)
            if resolved is None:
                continue
            yield resolved, Path(os.path.relpath(entry, root)).as_posix()


def matches_glob(relative: str, pattern: str) -> bool:
    """Match a workspace-relative path against a glob pattern.

    ``fnmatch`` wildcards cross ``/``, so the pattern shapes a model actually
    writes are normalized first: a leading ``**/`` matches at any depth, and a
    pattern naming no directory (``*.py``) is matched against the file name
    rather than the whole path.

    Known divergence from bash globstar, called out in the tool description so a
    worker does not silently miss files: a ``**`` in the MIDDLE of a pattern
    still spans a literal ``/``, so ``src/**/*.py`` requires an intermediate
    directory and does not match ``src/a.py``.

    Args:
        relative: The workspace-relative posix path.
        pattern: The glob pattern.

    Returns:
        Whether the path matches.
    """
    if pattern.startswith("**/"):
        pattern = pattern[3:]
    if "/" not in pattern:
        return fnmatch.fnmatch(os.path.basename(relative), pattern)
    return fnmatch.fnmatch(relative, pattern)


def _collect_hits(path: Path, relative: str, matcher: re.Pattern, hits: list[str]) -> bool:
    """Append one file's matching lines. Returns True once the match cap is hit."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        # A binary or unreadable file is skipped, not fatal: one such file in a
        # tree must not deny the model the rest of the search.
        return False
    for number, line in enumerate(text.splitlines(), start=1):
        if matcher.search(line):
            hits.append(f"{relative}:{number}:{line}")
            if len(hits) >= MAX_GREP_MATCHES:
                return True
    return False
