"""The rich tool set works end-to-end against a real temp workspace.

Each tool is driven by scripted fake-provider turns and asserted on its concrete
effect — the file on disk, the observation the model would read — never on how
the Runtime got there.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from chinamax import confinement
from chinamax.tools import search
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


def test_apply_patch_all_or_nothing(job_env, tmp_path, outside_root):
    """A second file that escapes both roots leaves the first one untouched."""
    escape = outside_root / "escape.txt"
    escape.write_text("secret\n", encoding="utf-8")
    # The climb is computed against job_env's workspace (always tmp_path/"workspace")
    # BEFORE job_env runs, so the escaping header resolves outside both roots even
    # though the target now lives under outside_root rather than beside the workspace.
    climb = os.path.relpath(escape, tmp_path / "workspace")
    patch = (
        "--- a/first.txt\n"
        "+++ b/first.txt\n"
        "@@ -1 +1 @@\n"
        "-one\n"
        "+ONE\n"
        f"--- a/{climb}\n"
        f"+++ b/{climb}\n"
        "@@ -1 +1 @@\n"
        "-secret\n"
        "+pwned\n"
    )
    env = job_env(tool_script(("apply_patch", {"patch": patch})))
    (env.workspace / "first.txt").write_text("one\n", encoding="utf-8")

    assert env.run() == 0

    assert (env.workspace / "first.txt").read_text(encoding="utf-8") == "one\n"
    assert escape.read_text(encoding="utf-8") == "secret\n"
    observations = env.observations()
    assert len(observations) == 1
    assert observations[0]["is_error"] is True
    assert "outside the workspace" in observations[0]["content"]


def test_scratch_root_tools_round_trip(job_env):
    """write/read/edit/list/glob all work against a private Scratch-root dir (0.6.0)."""
    scratch_dir = tempfile.mkdtemp(prefix="chinamax-test-", dir=confinement.SCRATCH_ROOT)
    target = Path(scratch_dir) / "f.txt"
    env = job_env(
        tool_script(
            ("write_file", {"path": str(target), "content": "one\n"}),
            ("read_file", {"path": str(target)}),
            ("str_replace_edit", {"path": str(target), "old_string": "one", "new_string": "two"}),
            ("list_dir", {"path": scratch_dir}),
            ("glob", {"pattern": "f.txt", "path": scratch_dir}),
        )
    )
    try:
        assert env.run() == 0

        observations = env.observations()
        # No observation errors — the write landed on disk AND reported success,
        # which is exactly what the _relative fix pins (without it the write
        # succeeds yet errors while formatting its message).
        assert not any(block.get("is_error") for block in observations)
        assert target.read_text(encoding="utf-8") == "two\n"
        # The scratch hit renders as the file's absolute path (walk_files rendering).
        assert observations[4]["content"] == str(target)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def test_glob_scan_cap(job_env, monkeypatch):
    """glob bounds the files it scans and says so, mirroring grep (ADR 0002)."""
    monkeypatch.setattr(search, "MAX_GLOB_FILES", 2)
    env = job_env(tool_script(("glob", {"pattern": "*.txt"})))
    for index in range(5):
        (env.workspace / f"f{index}.txt").write_text("x", encoding="utf-8")

    assert env.run() == 0

    observation = env.observations()[0]
    assert "stopped after scanning 2 files" in observation["content"]


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


# ── Policy hooks (ADR 0016) ────────────────────────────────────────────────────

import json

from conftest import (
    hook_group,
    policy_spec,
    write_claude_settings,
    write_hook_script,
    write_policy_settings,
)
from chinamax.policy import Policy, translate_tool


def _build_policy(workspace, transcript):
    return Policy.build(policy_spec(workspace, transcript))


@pytest.mark.parametrize(
    "native_name, native_input, claude_name, expected_extra",
    [
        ("bash", {"command": "ls"}, "Bash", {"command": "ls"}),
        ("read_file", {"path": "a", "offset": 2}, "Read", {"file_path": "a"}),
        ("write_file", {"path": "a", "content": "c"}, "Write", {"file_path": "a"}),
        ("str_replace_edit", {"path": "a", "old_string": "x", "new_string": "y"}, "Edit", {"file_path": "a"}),
        ("grep", {"pattern": "p", "path": ".", "include": "*.py", "fixed": True}, "Grep", {"glob": "*.py"}),
        ("glob", {"pattern": "*.py", "path": "."}, "Glob", {"pattern": "*.py"}),
        ("list_dir", {"path": "d"}, "Glob", {"pattern": "*"}),
    ],
)
def test_translate_tool_full_table(native_name, native_input, claude_name, expected_extra):
    """Every native tool presents with its Claude-canonical name and superset keys."""
    name, payload = translate_tool(native_name, native_input)
    assert name == claude_name
    for key, value in expected_extra.items():
        assert payload[key] == value
    # Unmapped native keys ride along unchanged (matchers see supersets).
    for key, value in native_input.items():
        assert payload[key] == value


def test_pretooluse_deny_blocks_dispatch(job_env, keyless_home):
    """A PreToolUse deny (exit 2) stops the tool and returns its reason verbatim."""
    write_policy_settings(hooks=True)
    hooks_dir = keyless_home / "hookdir"
    hooks_dir.mkdir()
    command = write_hook_script(hooks_dir, "deny", exit_code=2, stderr="nope, not that command")
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(command, matcher="Bash")],
    )
    env = job_env(tool_script(("bash", {"command": "echo hi > out.txt"})))

    assert env.run() == 0

    # The bash never ran: no file was written.
    assert "out.txt" not in env.tree()
    observations = env.observations()
    assert observations[0]["is_error"] is True
    assert "nope, not that command" in observations[0]["content"]


def test_hooks_off_never_fires_configured_hook(job_env, keyless_home):
    """The default-OFF hooks toggle: a configured settings-file hook never fires."""
    command = write_hook_script(keyless_home / "hookdir", "deny", exit_code=2, stderr="blocked")
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(command, matcher="Bash")],
    )
    env = job_env(tool_script(("bash", {"command": "echo hi > out.txt"})))

    assert env.run() == 0

    # Hooks OFF: the deny never fired, so the bash ran and wrote the file.
    assert "out.txt" in env.tree()
    assert not env.observations()[0].get("is_error", False)


def test_posttooluse_additional_context_appended(job_env, keyless_home):
    """PostToolUse additionalContext rides after the tool_result in the same turn."""
    write_policy_settings(hooks=True)
    marker = "POLICY-EXTRA-CONTEXT-7f3a"
    stdout = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": marker}}
    )
    command = write_hook_script(keyless_home / "hookdir", "ctx", exit_code=0, stdout=stdout)
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        post=[hook_group(command, matcher="Bash")],
    )
    env = job_env(tool_script(("bash", {"command": "echo hi > out.txt"})))

    assert env.run() == 0

    # The context landed in the turn the loop sent AFTER the bash result.
    assert any(marker in json.dumps(req["body"]) for req in env.requests)


def test_ask_decision_allows(keyless_home, tmp_path):
    """permissionDecision:'ask' resolves to allow (fail-open), not a block."""
    stdout = json.dumps(
        {"hookSpecificOutput": {"permissionDecision": "ask", "permissionDecisionReason": "hmm"}}
    )
    command = write_hook_script(tmp_path, "ask", exit_code=0, stdout=stdout)
    write_claude_settings(
        keyless_home / ".claude" / "settings.json", pre=[hook_group(command)]
    )
    policy = _build_policy(tmp_path, tmp_path / "t.jsonl")

    result = policy.pre_tool_use("Bash", {"command": "ls"})
    assert result.allowed is True


@pytest.mark.parametrize("exit_code, sleep_s, timeout", [(7, 0, None), (0, 2, 0.4)])
def test_hook_crash_or_timeout_continues(keyless_home, tmp_path, exit_code, sleep_s, timeout):
    """A crashing (nonzero) or timing-out PreToolUse hook fails open to allow."""
    command = write_hook_script(tmp_path, "bad", exit_code=exit_code, sleep_s=sleep_s)
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(command, timeout=timeout)],
    )
    policy = _build_policy(tmp_path, tmp_path / "t.jsonl")

    assert policy.pre_tool_use("Bash", {"command": "ls"}).allowed is True


def test_pretooluse_sequential_short_circuit(keyless_home, tmp_path):
    """The first deny short-circuits: a later hook in the event never runs."""
    first_evidence = tmp_path / "first.seen"
    second_evidence = tmp_path / "second.seen"
    deny = write_hook_script(tmp_path, "deny", exit_code=2, stderr="blocked", evidence=first_evidence)
    later = write_hook_script(tmp_path, "later", exit_code=0, evidence=second_evidence)
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(deny), hook_group(later)],
    )
    policy = _build_policy(tmp_path, tmp_path / "t.jsonl")

    result = policy.pre_tool_use("Bash", {"command": "ls"})
    assert result.allowed is False
    assert first_evidence.exists()
    assert not second_evidence.exists()


@pytest.mark.parametrize(
    "matcher, tool_name, should_run",
    [
        ("Bash", "Bash", True),
        ("Ba.h", "Bash", True),
        ("Bash", "Read", False),
        ("*", "Read", True),
        (None, "Read", True),
        ("[unclosed", "Bash", False),
    ],
)
def test_matcher_semantics(keyless_home, tmp_path, matcher, tool_name, should_run):
    """The matcher is a full regex over the TRANSLATED name; invalid regex skips."""
    evidence = tmp_path / "ran.seen"
    command = write_hook_script(tmp_path, "probe", exit_code=0, evidence=evidence)
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(command, matcher=matcher)],
    )
    policy = _build_policy(tmp_path, tmp_path / "t.jsonl")

    policy.pre_tool_use(tool_name, {"command": "ls"})
    assert evidence.exists() is should_run


def test_hook_sees_translated_payload(keyless_home, tmp_path):
    """The hook's stdin payload carries the translated name and superset input."""
    evidence = tmp_path / "payload.json"
    command = write_hook_script(tmp_path, "see", exit_code=0, evidence=evidence)
    write_claude_settings(
        keyless_home / ".claude" / "settings.json", pre=[hook_group(command)]
    )
    policy = _build_policy(tmp_path, tmp_path / "t.jsonl")

    name, payload = translate_tool("read_file", {"path": "notes.txt", "offset": 3})
    policy.pre_tool_use(name, payload)

    seen = json.loads(evidence.read_text(encoding="utf-8"))
    assert seen["hook_event_name"] == "PreToolUse"
    assert seen["tool_name"] == "Read"
    assert seen["tool_input"]["file_path"] == "notes.txt"
    assert seen["tool_input"]["path"] == "notes.txt"


def test_apply_patch_per_file_synthesis(keyless_home, tmp_path):
    """apply_patch synthesizes one Edit PreToolUse event per file, raw segment intact."""
    evidence_dir = tmp_path / "seen"
    evidence_dir.mkdir()
    # Each invocation appends its payload as one JSON line.
    log = evidence_dir / "events.jsonl"
    command = f"bash -c 'cat >> {json.dumps(str(log))}; printf \"\\n\" >> {json.dumps(str(log))}'"
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(command, matcher="Edit")],
    )
    policy = _build_policy(tmp_path, tmp_path / "t.jsonl")

    result = policy.pre_tool_use_patch(MODIFY_PATCH)
    assert result.allowed is True
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(events) == 2  # one Edit event per file the patch touches
    file_paths = [event["tool_input"]["file_path"] for event in events]
    assert file_paths == ["hello.py", "added.txt"]
    # The raw segment is re-sliced by file boundary — the second file's hunk bytes
    # ride with its own event, never the whole patch.
    assert "brand new" in events[1]["tool_input"]["patch"]
    assert "brand new" not in events[0]["tool_input"]["patch"]


def test_apply_patch_single_deny_vetoes(job_env, keyless_home):
    """Any single per-file PreToolUse deny vetoes the WHOLE patch (nothing applied)."""
    write_policy_settings(hooks=True)
    command = write_hook_script(keyless_home / "hookdir", "veto", exit_code=2, stderr="no edits")
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(command, matcher="Edit")],
    )
    env = job_env(tool_script(("apply_patch", {"patch": MODIFY_PATCH})))
    (env.workspace / "hello.py").write_text('def hello():\n    return "hello"\n', encoding="utf-8")

    assert env.run() == 0

    # The veto landed before ApplyPatch.execute: hello.py is untouched and added.txt
    # was never created.
    assert (env.workspace / "hello.py").read_text(encoding="utf-8") == 'def hello():\n    return "hello"\n'
    assert "added.txt" not in env.tree()
    assert env.observations()[0]["is_error"] is True


def test_disable_all_hooks_silences_the_job(keyless_home, tmp_path):
    """disableAllHooks in any Claude source drops every Policy hook for the Job."""
    evidence = tmp_path / "ran.seen"
    command = write_hook_script(tmp_path, "probe", exit_code=2, stderr="deny", evidence=evidence)
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(command)],
        disable_all=True,
    )
    policy = _build_policy(tmp_path, tmp_path / "t.jsonl")

    assert policy.pre_tool_use("Bash", {"command": "ls"}).allowed is True
    assert not evidence.exists()
