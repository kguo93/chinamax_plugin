"""The apply_patch tool: a transactional unified-diff applier.

Transactional means all-or-nothing across the whole diff. Every path is resolved
and every hunk applied against in-memory contents FIRST; only once every file
has a result do the writes happen, staged to temp files beside their targets and
renamed into place. A rejection on file 3 of 5 therefore leaves the workspace
untouched — dry-running alone would not deliver that, because the writes would
still happen one at a time; the staging step is what makes the claim true. Only
a crash in the middle of the rename sequence can split a commit.

Context matching is strict: no fuzz, no offset search. A hunk whose context does
not sit exactly where its header says is a rejected patch, not a patch to guess
at. git rename, copy and mode-change headers are refused outright rather than
silently ignored, which would apply a rename as a truncating overwrite.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from chinamax import ToolError, state
from chinamax.confinement import ToolContext, resolve_in_workspace

#: Suffix of the temp file each write is staged to before being renamed in.
STAGE_SUFFIX = ".chinamax-patch-tmp"

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_UNSUPPORTED_HEADERS = ("rename from ", "rename to ", "copy from ", "copy to ",
                        "old mode ", "new mode ")

APPLY_PATCH_TOOL = {
    "name": "apply_patch",
    "description": (
        "Apply a unified diff to the workspace. 'a/' and 'b/' path prefixes are "
        "stripped, and a '/dev/null' header creates or deletes a file. Context must "
        "match exactly — no fuzz — and the whole diff applies or none of it does. "
        "git rename, copy and mode-change headers are not supported."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"patch": {"type": "string"}},
        "required": ["patch"],
    },
}


@dataclass
class Hunk:
    """One hunk: where it applies and the body lines that make it up."""

    old_start: int
    old_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class FileEdit:
    """One file's worth of diff. A None path is ``/dev/null``."""

    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    no_newline_at_eof: bool = False


class ApplyPatch:
    """Apply a whole unified diff, or none of it."""

    spec = APPLY_PATCH_TOOL
    writes = True

    def execute(self, value: dict, context: ToolContext) -> str:
        """Resolve, dry-run, then stage and commit every file in the diff."""
        edits = parse_patch(value["patch"])
        writes: list[tuple[Path, str, str]] = []
        deletions: list[tuple[Path, str]] = []
        for edit in edits:
            self._plan(edit, context, writes, deletions)

        staged: list[tuple[Path, Path]] = []
        for path, text, _ in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + STAGE_SUFFIX)
            temporary.write_text(text, encoding="utf-8")
            staged.append((temporary, path))
        for temporary, path in staged:
            state.atomic_replace(temporary, path)
        for path, _ in deletions:
            path.unlink()

        touched = [name for _, _, name in writes] + [name for _, name in deletions]
        return f"applied the patch to {len(touched)} file(s): {', '.join(touched)}"

    def _plan(
        self,
        edit: FileEdit,
        context: ToolContext,
        writes: list[tuple[Path, str, str]],
        deletions: list[tuple[Path, str]],
    ) -> None:
        """Resolve one file edit and dry-run its hunks, changing nothing on disk."""
        if edit.old_path is not None and edit.new_path is not None and edit.old_path != edit.new_path:
            raise ToolError(
                f"the patch renames {edit.old_path!r} to {edit.new_path!r}; renames are "
                "not supported — write the new file and delete the old one instead"
            )
        target = edit.new_path if edit.new_path is not None else edit.old_path
        if target is None:
            raise ToolError("a file header names /dev/null on both sides")
        path = resolve_in_workspace(context.root, target)

        if edit.old_path is None:
            original, trailing = [], True
            if path.exists():
                raise ToolError(f"the patch creates {target}, but that file already exists")
        else:
            if not path.is_file():
                raise ToolError(f"the patch changes {target}, but that file does not exist")
            text = path.read_text(encoding="utf-8")
            original, trailing = text.splitlines(), text.endswith("\n")

        updated = _apply_hunks(original, edit.hunks, target)
        if edit.new_path is None:
            if updated:
                raise ToolError(
                    f"the patch deletes {target}, but {len(updated)} line(s) would remain"
                )
            deletions.append((path, target))
            return
        if edit.no_newline_at_eof:
            trailing = False
        writes.append((path, "\n".join(updated) + ("\n" if trailing and updated else ""), target))


def parse_patch(text: str) -> list[FileEdit]:
    """Parse a unified diff into per-file edits.

    Args:
        text: The diff as the model wrote it.

    Returns:
        One `FileEdit` per file header, in file order.

    Raises:
        ToolError: On a malformed diff, or one carrying an unsupported git header.
    """
    lines = text.split("\n")
    edits: list[FileEdit] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        for header in _UNSUPPORTED_HEADERS:
            if line.startswith(header):
                raise ToolError(
                    f"unsupported git header {line.strip()!r}: apply_patch handles "
                    "content diffs only, not renames, copies or mode changes"
                )
        if line.startswith("--- "):
            edit, index = _parse_file(lines, index)
            edits.append(edit)
            continue
        index += 1
    if not edits:
        raise ToolError(
            "no unified-diff file headers found; the patch needs '--- ' and '+++ ' "
            "lines followed by '@@' hunks"
        )
    return edits


def _parse_file(lines: list[str], index: int) -> tuple[FileEdit, int]:
    """Parse one file's header and hunks, returning the edit and the next index."""
    old_path = _header_path(lines[index][4:])
    index += 1
    if index >= len(lines) or not lines[index].startswith("+++ "):
        raise ToolError(f"the '--- {old_path}' header is not followed by a '+++ ' header")
    new_path = _header_path(lines[index][4:])
    index += 1

    edit = FileEdit(old_path=old_path, new_path=new_path)
    while index < len(lines) and lines[index].startswith("@@"):
        hunk, index = _parse_hunk(lines, index, edit)
        edit.hunks.append(hunk)
    if not edit.hunks:
        raise ToolError(f"the diff for {new_path or old_path} has no '@@' hunks")
    return edit, index


def _parse_hunk(lines: list[str], index: int, edit: FileEdit) -> tuple[Hunk, int]:
    """Parse one hunk, consuming exactly the body its header declares.

    The declared line counts are what makes the body unambiguous: a `+++ b/x`
    line of content is otherwise indistinguishable from the next file's header.
    """
    match = _HUNK_HEADER.match(lines[index])
    if match is None:
        raise ToolError(f"malformed hunk header: {lines[index]!r}")
    old_start, old_count, _, new_count = (
        int(match.group(1)),
        1 if match.group(2) is None else int(match.group(2)),
        int(match.group(3)),
        1 if match.group(4) is None else int(match.group(4)),
    )
    index += 1

    hunk = Hunk(old_start=old_start, old_count=old_count)
    seen_old = seen_new = 0
    while seen_old < old_count or seen_new < new_count:
        if index >= len(lines):
            raise ToolError(
                f"the hunk at line {old_start} ends before its declared "
                f"-{old_count}/+{new_count} lines"
            )
        raw = lines[index]
        index += 1
        if raw.startswith("\\"):
            if hunk.lines:
                _mark_no_newline(hunk.lines[-1], edit)
            continue
        # A blank context line is often written without its leading space.
        line = raw if raw else " "
        kind = line[0]
        if kind == " ":
            seen_old += 1
            seen_new += 1
        elif kind == "-":
            seen_old += 1
        elif kind == "+":
            seen_new += 1
        else:
            raise ToolError(f"unexpected line in a hunk body: {raw!r}")
        hunk.lines.append(line)
    if index < len(lines) and lines[index].startswith("\\"):
        _mark_no_newline(hunk.lines[-1], edit)
        index += 1
    return hunk, index


def _mark_no_newline(previous: str, edit: FileEdit) -> None:
    """Record a '\\ No newline at end of file' that applies to the new side."""
    if previous[0] in {" ", "+"}:
        edit.no_newline_at_eof = True


def _header_path(raw: str) -> str | None:
    """Strip a header's timestamp and ``a/``/``b/`` prefix; None for /dev/null."""
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _apply_hunks(original: list[str], hunks: list[Hunk], target: str) -> list[str]:
    """Apply every hunk to a file's lines in memory, matching context exactly.

    Args:
        original: The file's current lines, without line endings.
        hunks: The file's hunks, in order.
        target: The file name, for error messages.

    Returns:
        The updated lines.

    Raises:
        ToolError: On a hunk whose context does not match exactly where it says.
    """
    updated: list[str] = []
    cursor = 0
    for hunk in hunks:
        start = hunk.old_start - 1 if hunk.old_count > 0 else hunk.old_start
        if start < cursor or start > len(original):
            raise ToolError(
                f"the hunk at {target}:{hunk.old_start} does not fit: the file has "
                f"{len(original)} line(s) and earlier hunks reached line {cursor}"
            )
        updated.extend(original[cursor:start])
        cursor = start
        for line in hunk.lines:
            kind, text = line[0], line[1:]
            if kind == "+":
                updated.append(text)
                continue
            if cursor >= len(original) or original[cursor] != text:
                found = original[cursor] if cursor < len(original) else "(end of file)"
                raise ToolError(
                    f"context mismatch at {target}:{cursor + 1}: the patch expects "
                    f"{text!r} but the file has {found!r}"
                )
            if kind == " ":
                updated.append(text)
            cursor += 1
    updated.extend(original[cursor:])
    return updated
