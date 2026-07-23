"""The rich tool set works end-to-end against a real temp workspace.

Each tool is driven by scripted fake-provider turns and asserted on its concrete
effect — the file on disk, the observation the model would read — never on how
the Runtime got there.
"""

from __future__ import annotations

import pytest

from conftest import REPORT_PAYLOAD, tool_script

MODIFY_PATCH = """--- a/hello.py
+++ b/hello.py
@@ -1,2 +1,2 @@
 def hello():
-    return "hello"
+    return "hi"
--- /dev/null
+++ b/added.txt
@@ -0,0 +1 @@
+brand new
"""

ESCAPING_PATCH = """--- a/first.txt
+++ b/first.txt
@@ -1 +1 @@
-one
+ONE
--- a/../escape.txt
+++ b/../escape.txt
@@ -1 +1 @@
-secret
+pwned
"""


def test_read_file_happy_path(job_env):
    """read_file returns the file verbatim, and a 1-based offset/limit slice of it."""
    env = job_env(
        tool_script(
            ("read_file", {"path": "notes.txt"}),
            ("read_file", {"path": "notes.txt", "offset": 2, "limit": 1}),
        )
    )
    (env.workspace / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    assert env.run() == 0

    observations = env.observations()
    assert observations[0]["content"] == "alpha\nbeta\ngamma\n"
    assert observations[1]["content"] == "beta\n"
    assert not any(block.get("is_error") for block in observations)


def test_write_file_happy_path(job_env):
    """write_file creates the file and any missing parents inside the workspace."""
    env = job_env(tool_script(("write_file", {"path": "sub/deep/new.txt", "content": "made\n"})))

    assert env.run() == 0

    assert (env.workspace / "sub" / "deep" / "new.txt").read_text(encoding="utf-8") == "made\n"
    assert "sub/deep/new.txt" in env.observations()[0]["content"]


def test_str_replace_edit_happy_path(job_env):
    """A unique literal is replaced; an ambiguous one is refused and changes nothing."""
    env = job_env(
        tool_script(
            ("str_replace_edit", {"path": "conf.ini", "old_string": "port = 80", "new_string": "port = 443"}),
            ("str_replace_edit", {"path": "conf.ini", "old_string": "repeated", "new_string": "x"}),
        )
    )
    (env.workspace / "conf.ini").write_text(
        "port = 80\nrepeated\nrepeated\n", encoding="utf-8"
    )

    assert env.run() == 0

    assert (env.workspace / "conf.ini").read_text(encoding="utf-8") == (
        "port = 443\nrepeated\nrepeated\n"
    )
    observations = env.observations()
    assert not observations[0].get("is_error")
    assert observations[1]["is_error"] is True
    assert "occurs 2 times" in observations[1]["content"]


def test_list_dir_happy_path(job_env):
    """list_dir lists one level, marking directories."""
    env = job_env(tool_script(("list_dir", {"path": "."})))
    (env.workspace / "a.txt").write_text("a", encoding="utf-8")
    (env.workspace / "b.txt").write_text("b", encoding="utf-8")
    (env.workspace / "sub").mkdir()
    (env.workspace / "sub" / "buried.txt").write_text("c", encoding="utf-8")

    assert env.run() == 0

    assert env.observations()[0]["content"] == "a.txt\nb.txt\nsub/"


def test_grep_happy_path(job_env):
    """grep returns path:line:text hits, filtered by 'include' and literal with 'fixed'."""
    env = job_env(
        tool_script(
            ("grep", {"pattern": "def ma.n"}),
            ("grep", {"pattern": "main", "include": "*.py"}),
            ("grep", {"pattern": "def ma.n", "fixed": True}),
        )
    )
    (env.workspace / "src").mkdir()
    (env.workspace / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (env.workspace / "README.md").write_text("main is the entry point\n", encoding="utf-8")

    assert env.run() == 0

    observations = env.observations()
    assert observations[0]["content"] == "src/app.py:1:def main():"
    assert observations[1]["content"] == "src/app.py:1:def main():"
    assert "no matches" in observations[2]["content"]


def test_glob_happy_path(job_env):
    """glob matches at any depth for a bare pattern and honours an explicit one."""
    env = job_env(
        tool_script(
            ("glob", {"pattern": "**/*.py"}),
            ("glob", {"pattern": "*.md"}),
            ("glob", {"pattern": "src/*.py"}),
        )
    )
    (env.workspace / "src").mkdir()
    (env.workspace / "src" / "app.py").write_text("x", encoding="utf-8")
    (env.workspace / "README.md").write_text("x", encoding="utf-8")

    assert env.run() == 0

    observations = env.observations()
    assert observations[0]["content"] == "src/app.py"
    assert observations[1]["content"] == "README.md"
    assert observations[2]["content"] == "src/app.py"


def test_apply_patch_happy_path(job_env):
    """A two-file diff edits one file and creates the other from /dev/null."""
    env = job_env(tool_script(("apply_patch", {"patch": MODIFY_PATCH})))
    (env.workspace / "hello.py").write_text('def hello():\n    return "hello"\n', encoding="utf-8")

    assert env.run() == 0

    assert (env.workspace / "hello.py").read_text(encoding="utf-8") == (
        'def hello():\n    return "hi"\n'
    )
    assert (env.workspace / "added.txt").read_text(encoding="utf-8") == "brand new\n"
    assert not env.observations()[0].get("is_error")


def test_apply_patch_all_or_nothing(job_env):
    """A second file that escapes the workspace leaves the first one untouched."""
    env = job_env(tool_script(("apply_patch", {"patch": ESCAPING_PATCH})))
    (env.workspace / "first.txt").write_text("one\n", encoding="utf-8")
    escape = env.workspace.parent / "escape.txt"
    escape.write_text("secret\n", encoding="utf-8")

    assert env.run() == 0

    assert (env.workspace / "first.txt").read_text(encoding="utf-8") == "one\n"
    assert escape.read_text(encoding="utf-8") == "secret\n"
    observations = env.observations()
    assert len(observations) == 1
    assert observations[0]["is_error"] is True
    assert "outside the workspace" in observations[0]["content"]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(("read_file", {"path": "blob.bin"}), id="undecodable_file"),
        pytest.param(("grep", {"pattern": "(unclosed"}), id="invalid_regex"),
    ],
)
def test_tool_exception_becomes_observation(job_env, call):
    """A tool that raises yields an error observation and the loop proceeds."""
    env = job_env(tool_script(call))
    (env.workspace / "blob.bin").write_bytes(b"\xff\xfe\x00\x01\x80")

    assert env.run() == 0

    observations = env.observations()
    assert len(observations) == 1
    assert observations[0]["is_error"] is True
    assert observations[0]["tool_use_id"] == "toolu_0"
    # The loop reached the scripted turn after the failure.
    assert env.result() == REPORT_PAYLOAD
    assert len(env.requests) == 2
