"""A read-only Job cannot edit: write tools are gone from schema AND dispatch."""

from __future__ import annotations

import os
import tempfile

from chinamax import confinement
from conftest import REPORT_PAYLOAD, bash_script, tool_script


def test_write_tools_absent_from_schema(job_env):
    """The advertised tool list drops write_file, str_replace_edit and apply_patch."""
    env = job_env(bash_script("ls"))

    assert env.run(env.spec(write=False)) == 0

    assert [tool["name"] for tool in env.requests[0]["body"]["tools"]] == [
        "bash",
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "report_result",
    ]


def test_unadvertised_write_tool_rejected_at_dispatch(job_env):
    """Calling a filtered-out tool — or one never registered — is refused, not executed."""
    env = job_env(
        tool_script(
            ("write_file", {"path": "new.txt", "content": "pwned"}),
            ("teleport", {"destination": "mars"}),
        )
    )

    assert env.run(env.spec(write=False)) == 0

    assert not (env.workspace / "new.txt").exists()
    observations = env.observations()
    assert [block["is_error"] for block in observations] == [True, True]
    assert "unknown tool 'write_file'" in observations[0]["content"]
    assert "unknown tool 'teleport'" in observations[1]["content"]
    # Both refusals were observations: the loop carried on to the terminal turn.
    assert env.result() == REPORT_PAYLOAD


def test_write_shaped_bash_blocked(job_env):
    """Common file-writing shell forms are refused in a read-only Job."""
    env = job_env(bash_script("echo x > f", "echo x>f", "mv f /tmp/", "touch f"))

    assert env.run(env.spec(write=False)) == 0

    observations = env.observations()
    assert [block["is_error"] for block in observations] == [True, True, True, True]
    for observation in observations:
        assert "read-only" in observation["content"]
        assert "was not executed" in observation["content"]
    assert env.tree() == []


def test_read_bash_allowed(job_env):
    """Reads run untouched, including the redirections that only move file descriptors."""
    env = job_env(bash_script("ls", "ls missing 2>/dev/null", "grep x f 2>&1"))
    (env.workspace / "f").write_text("x\n", encoding="utf-8")

    assert env.run(env.spec(write=False)) == 0

    observations = env.observations()
    assert not any(block.get("is_error") for block in observations)
    assert "f" in observations[0]["content"]
    assert observations[1]["content"].startswith("exit_code: ")
    assert "x" in observations[2]["content"]


def test_read_only_job_reads_scratch_root_file(job_env):
    """A read-only Job's read tool can read a file under the Scratch root (0.6.0 carve-out)."""
    fd, created = tempfile.mkstemp(prefix="chinamax-test-", dir=confinement.SCRATCH_ROOT)
    os.write(fd, b"scratch-visible\n")
    os.close(fd)
    env = job_env(tool_script(("read_file", {"path": created})))
    try:
        assert env.run(env.spec(write=False)) == 0

        observation = env.observations()[0]
        assert not observation.get("is_error")
        assert observation["content"] == "scratch-visible\n"
    finally:
        os.unlink(created)


# ── Worker Host-policy in read-only Jobs (ADR 0016) ────────────────────────────

from conftest import (
    hook_group,
    mcp_server_entry,
    mcp_server_script,
    write_claude_settings,
    write_hook_script,
    write_mcp_config,
)


def test_mcp_tools_advertised_in_read_only_job(job_env, tmp_path):
    """Worker MCP tools stay advertised in a read-only Job (outside the posture)."""
    script = mcp_server_script(tmp_path)
    env = job_env(bash_script("ls"))
    (env.workspace / ".claude").mkdir()
    write_mcp_config(env.workspace / ".mcp.json", {"echo": mcp_server_entry(script)})

    assert env.run(env.spec(write=False)) == 0

    names = [tool["name"] for tool in env.requests[0]["body"]["tools"]]
    assert "mcp__echo__echo" in names
    # The write tools are still gone — the posture governs native tools only.
    assert "write_file" not in names


def test_hooks_fire_in_read_only_job(job_env, keyless_home):
    """Policy hooks run even for a read-only Job (they are host policy, not tools)."""
    command = write_hook_script(keyless_home / "hookdir", "deny", exit_code=2, stderr="hook says no")
    write_claude_settings(
        keyless_home / ".claude" / "settings.json",
        pre=[hook_group(command, matcher="Bash")],
    )
    env = job_env(bash_script("ls"))

    assert env.run(env.spec(write=False)) == 0

    observations = env.observations()
    assert observations[0]["is_error"] is True
    assert "hook says no" in observations[0]["content"]
