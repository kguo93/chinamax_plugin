"""A read-only Job cannot edit: write tools are gone from schema AND dispatch."""

from __future__ import annotations

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
