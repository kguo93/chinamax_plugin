"""The file tools: read_file, write_file, str_replace_edit and list_dir.

Every path argument goes through `resolve_in_workspace` — that call is the
confinement, and there is no other way for these tools to turn a model-supplied
string into a path. Files are read and written as strict UTF-8: a binary file
raises, and the registry renders the failure as an error observation rather than
handing the model bytes that decoded into replacement characters.
"""

from __future__ import annotations

from pathlib import Path

from chinamax import ToolError
from chinamax.confinement import ToolContext, contained, resolve_in_workspace

READ_FILE_TOOL = {
    "name": "read_file",
    "description": (
        "Read a UTF-8 text file from the workspace. Optional 1-based 'offset' and "
        "'limit' select a range of lines; the contents are returned as they are on "
        "disk, so they can be used verbatim in a str_replace_edit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    },
}

WRITE_FILE_TOOL = {
    "name": "write_file",
    "description": (
        "Create a file in the workspace or overwrite it whole, creating any missing "
        "parent directories. Use str_replace_edit to change part of an existing file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    },
}

STR_REPLACE_EDIT_TOOL = {
    "name": "str_replace_edit",
    "description": (
        "Replace one literal string in a workspace file. 'old_string' is matched "
        "literally, not as a regular expression, and must occur exactly once in the "
        "file — include surrounding lines to make it unique."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        "required": ["path", "old_string", "new_string"],
    },
}

LIST_DIR_TOOL = {
    "name": "list_dir",
    "description": (
        "List one directory level of the workspace. Directories are marked with a "
        "trailing '/'. Entries that resolve outside the workspace are omitted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    },
}


class ReadFile:
    """Return a workspace file's text, optionally a range of its lines."""

    spec = READ_FILE_TOOL
    writes = False

    def execute(self, value: dict, context: ToolContext) -> str:
        """Read the file and slice it to the requested line range."""
        path = resolve_in_workspace(context.root, value["path"])
        text = read_text(path)
        offset = value.get("offset")
        limit = value.get("limit")
        if offset is None and limit is None:
            return text if text else "(empty file)"

        lines = text.splitlines(keepends=True)
        start = 1 if offset is None else offset
        if start < 1:
            raise ToolError("'offset' is a 1-based line number, so it must be at least 1")
        if limit is not None and limit < 1:
            raise ToolError("'limit' must be at least 1")
        selected = lines[start - 1 :] if limit is None else lines[start - 1 : start - 1 + limit]
        if not selected:
            raise ToolError(
                f"{_relative(path, context)} has {len(lines)} line(s); "
                f"line {start} is past the end of the file"
            )
        return "".join(selected)


class WriteFile:
    """Create or overwrite a workspace file whole."""

    spec = WRITE_FILE_TOOL
    writes = True

    def execute(self, value: dict, context: ToolContext) -> str:
        """Write the file, creating its parent directories inside the workspace."""
        path = resolve_in_workspace(context.root, value["path"])
        if path.is_dir():
            raise ToolError(f"{_relative(path, context)} is a directory, not a file")
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value["content"], encoding="utf-8")
        verb = "overwrote" if existed else "created"
        return f"{verb} {_relative(path, context)} ({len(value['content'])} characters)"


class StrReplaceEdit:
    """Replace one literal, uniquely occurring string in a workspace file."""

    spec = STR_REPLACE_EDIT_TOOL
    writes = True

    def execute(self, value: dict, context: ToolContext) -> str:
        """Replace the single occurrence, refusing zero or several."""
        path = resolve_in_workspace(context.root, value["path"])
        old_string = value["old_string"]
        if not old_string:
            raise ToolError("'old_string' must not be empty")
        text = read_text(path)
        occurrences = text.count(old_string)
        relative = _relative(path, context)
        if occurrences == 0:
            raise ToolError(
                f"'old_string' does not occur in {relative}; read the file and copy "
                "the text to replace exactly, whitespace included"
            )
        if occurrences > 1:
            raise ToolError(
                f"'old_string' occurs {occurrences} times in {relative}; include "
                "surrounding lines so it matches exactly one place"
            )
        path.write_text(text.replace(old_string, value["new_string"]), encoding="utf-8")
        return f"replaced 1 occurrence in {relative}"


class ListDir:
    """List one directory level, omitting entries that resolve outside."""

    spec = LIST_DIR_TOOL
    writes = False

    def execute(self, value: dict, context: ToolContext) -> str:
        """List the directory, re-validating every entry it discovers."""
        path = resolve_in_workspace(context.root, value.get("path") or ".")
        if not path.is_dir():
            raise ToolError(f"{_relative(path, context)} is not a directory")
        entries = []
        for entry in sorted(path.iterdir(), key=lambda item: item.name):
            # An entry can itself be a symlink out of the workspace, so the walk
            # re-validates it rather than trusting its parent's containment.
            resolved = contained(context.root, entry)
            if resolved is None:
                continue
            entries.append(f"{entry.name}/" if resolved.is_dir() else entry.name)
        if not entries:
            return f"{_relative(path, context)} is empty"
        return "\n".join(entries)


def read_text(path: Path) -> str:
    """Read a workspace file as strict UTF-8.

    Args:
        path: A path already proven to be inside the workspace.

    Returns:
        The file's text.

    Raises:
        ToolError: If the path does not name a readable file.
        UnicodeDecodeError: If the file is not UTF-8 text; the registry renders
            it as an error observation.
    """
    if path.is_dir():
        raise ToolError(f"{path.name} is a directory, not a file")
    if not path.exists():
        raise ToolError(f"no such file: {path}")
    return path.read_text(encoding="utf-8")


def _relative(path: Path, context: ToolContext) -> str:
    """Render a resolved path relative to the workspace, for the observation."""
    if path == context.root:
        return "."
    return str(path.relative_to(context.root))
