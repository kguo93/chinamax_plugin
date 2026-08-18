from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path

import pytest

from chinamax.codex import (
    CodexPermissionError,
    bridge_name,
    exact_bridge_address,
    require_bypass_permissions,
    slugify_task_name,
    spawn_spec,
)
from chinamax.doctor import compile_codex_agent
from chinamax.hooks import codex_pretool
from chinamax.host import Host, HostContext, set_current_host


def test_codex_task_invariants_are_deterministic():
    assert slugify_task_name("Fix API / tests") == "fix_api_tests"
    assert bridge_name("deepseek", "Fix API / tests") == "chinamax_deepseek_fix_api_tests"
    assert bridge_name(
        "deepseek", "Fix API / tests", existing={"chinamax_deepseek_fix_api_tests"}
    ).startswith("chinamax_deepseek_fix_api_tests_")
    assert spawn_spec("deepseek", "Fix API") == {
        "task_name": "chinamax_deepseek_fix_api",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "low",
        "fork_turns": "none",
    }
    assert exact_bridge_address(
        "please steer chinamax_deepseek_fix_api", {"chinamax_deepseek_fix_api"}
    ) == "chinamax_deepseek_fix_api"
    assert exact_bridge_address("please steer deepseek", {"chinamax_deepseek_fix_api"}) is None


def test_codex_mutation_requires_yolo():
    with pytest.raises(CodexPermissionError):
        require_bypass_permissions("default")
    require_bypass_permissions("bypassPermissions")


def test_codex_pretool_is_noop_for_claude_and_blocks_mutation(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"host": "claude", "tool_name": "Agent"})),
    )
    assert codex_pretool.main() == 0
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"host": "codex", "tool_name": "Agent", "permission_mode": "default"}
            )
        ),
    )
    monkeypatch.setenv("CHINAMAX_HOST", "codex")
    assert codex_pretool.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert "codex --yolo" in payload["reason"]
    # Library-level tests share a Python process; leave the compatibility
    # default on Claude for the existing suite's direct Runtime helpers.
    set_current_host(HostContext.from_host(Host.CLAUDE))


def test_compiled_agent_has_codex_native_settings(tmp_path):
    source = tmp_path / "agents" / "chinamax.md"
    source.parent.mkdir()
    source.write_text(
        "---\nname: chinamax\ndescription: test adapter\n---\n\nLoad the canonical skill.\n",
        encoding="utf-8",
    )
    contract = "# Bridge contract\n\nKeep \\\"exact\\\" bytes. Δ\n"
    contract_path = tmp_path / "skills" / "chinamax-bridge" / "SKILL.md"
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text(contract, encoding="utf-8")
    compiled = compile_codex_agent(source)
    parsed = tomllib.loads(compiled)
    assert "# chinamax-managed-plugin-version: 0.7.4" in compiled
    assert 'model = "gpt-5.6-terra"' in compiled
    assert 'model_reasoning_effort = "low"' in compiled
    assert parsed["developer_instructions"] == (
        "Load the canonical skill.\n\n" + contract
    )
    assert parsed["developer_instructions"].endswith(contract)
    assert parsed["developer_instructions"].count(contract) == 1


def test_compiled_agent_requires_the_canonical_bridge_contract(tmp_path):
    source = tmp_path / "agents" / "chinamax.md"
    source.parent.mkdir()
    source.write_text(
        "---\nname: chinamax\ndescription: test adapter\n---\n\nAdapter.\n",
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError):
        compile_codex_agent(source)


def test_codex_task_skill_preserves_read_only_posture():
    skill = Path(__file__).parents[1] / "skills" / "chinamax-task" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "optional `--read-only` posture" in text
    assert "Preserve `--read-only` in the Runtime dispatch" in text
    assert "model: gpt-5.6-terra" in text
    assert "Bridge model" in text
    assert "Profile model" in text
    assert "never" in text.lower()  # the never-feed rule is present
    assert "Never copy it into the Runtime dispatch" in text


# ── Codex Host Policy hooks and Memory (ADR 0016) ──────────────────────────────

from chinamax import policy as _policy
from chinamax.policy import HookSpec, Policy, discover_hooks, discover_memory
from conftest import policy_spec, write_hook_script


def test_codex_config_toml_nested_hooks_run_all_supported_events(
    keyless_home, tmp_path
):
    """Nested Codex command handlers run for each Runtime Policy seam."""
    codex_home = keyless_home / ".codex"
    codex_home.mkdir()
    pre_allow_evidence = tmp_path / "pre-allow.seen"
    pre_deny_evidence = tmp_path / "pre-deny.seen"
    async_evidence = tmp_path / "async.seen"
    malformed_async_evidence = tmp_path / "malformed-async.seen"
    prompt_evidence = tmp_path / "prompt.seen"
    post_evidence = tmp_path / "post.seen"
    stop_evidence = tmp_path / "stop.seen"
    pre_allow = write_hook_script(
        tmp_path, "pre_allow", evidence=pre_allow_evidence
    )
    pre_deny = write_hook_script(
        tmp_path,
        "pre_deny",
        exit_code=2,
        stderr="codex deny",
        evidence=pre_deny_evidence,
    )
    async_command = write_hook_script(tmp_path, "async", evidence=async_evidence)
    malformed_async = write_hook_script(
        tmp_path, "malformed_async", evidence=malformed_async_evidence
    )
    prompt_command = write_hook_script(tmp_path, "prompt", evidence=prompt_evidence)
    post = write_hook_script(tmp_path, "post", evidence=post_evidence)
    stop = write_hook_script(
        tmp_path, "stop", exit_code=2, stderr="codex stop", evidence=stop_evidence
    )
    config = (
        "[features]\n"
        "hooks = false\n\n"
        "[hooks.state]\n"
        'trusted_hash = "deadbeef"\n\n'
        "[[hooks.PreToolUse]]\n"
        'matcher = "Bash"\n'
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(pre_allow)}\n"
        "timeout = 7\n"
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(pre_deny)}\n"
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(async_command)}\n"
        "async = true\n"
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(malformed_async)}\n"
        'async = "yes"\n'
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "prompt"\n'
        f"command = {json.dumps(prompt_command)}\n\n"
        "[[hooks.PreToolUse]]\n"
        'matcher = "Bash"\n'
        f"command = {json.dumps(prompt_command)}\n\n"
        "[[hooks.PostToolUse]]\n"
        'matcher = "Bash"\n'
        "[[hooks.PostToolUse.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(post)}\n"
        f"commandWindows = {json.dumps('cmd /d /c echo post')}\n"
        f"command_windows = {json.dumps('cmd /d /c echo alias')}\n"
        "timeout = 9\n"
        "[[hooks.PostToolUse.hooks]]\n"
        'type = "command"\n'
        f"commandWindows = {json.dumps('cmd /d /c echo windows-only')}\n\n"
        "[[hooks.Stop]]\n"
        "[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(stop)}\n"
    )
    (codex_home / "config.toml").write_text(config, encoding="utf-8")
    ctx = HostContext.from_host(Host.CODEX)

    logs: list[str] = []
    hooks = discover_hooks(tmp_path, ctx, logs.append)
    assert len(hooks.pre) == 2
    assert hooks.pre[0].matcher == "Bash"
    assert hooks.pre[0].timeout_s == 7
    assert len(hooks.post) == 2
    assert hooks.post[0].command_windows == "cmd /d /c echo post"
    assert hooks.post[0].timeout_s == 9
    assert hooks.post[1].command is None
    assert hooks.post[1].command_windows == "cmd /d /c echo windows-only"
    assert len(hooks.stop) == 1
    assert any("async" in message for message in logs)
    assert any("malformed async" in message for message in logs)
    assert any("unsupported" in message for message in logs)

    alias_specs = _policy._codex_hook_specs(
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "echo alias",
                                "command_windows": "cmd /d /c echo alias",
                            }
                        ]
                    }
                ]
            }
        },
        "PostToolUse",
        "codex",
        logs.append,
    )
    assert alias_specs[0].command_windows == "cmd /d /c echo alias"

    policy = Policy.build(
        policy_spec(tmp_path, tmp_path / "t.jsonl", host="codex"), host_context=ctx
    )
    result = policy.pre_tool_use("Bash", {"command": "ls"})
    # Supported handlers fire despite features.hooks=false and a trusted_hash gate.
    assert result.allowed is False
    assert pre_allow_evidence.exists()
    assert pre_deny_evidence.exists()
    assert not async_evidence.exists()
    assert not malformed_async_evidence.exists()
    assert not prompt_evidence.exists()

    assert policy.post_tool_use("Bash", {"command": "ls"}, "ok", False) == []
    assert post_evidence.exists()
    stop_result = policy.stop(False)
    assert stop_result.blocked is True
    assert "codex stop" in (stop_result.reason or "")
    assert stop_evidence.exists()


def test_codex_commandwindows_preferred_on_windows(monkeypatch):
    """On Windows a Codex commandWindows runs via cmd.exe, never the bash resolver."""
    spec = HookSpec(
        command="bash payload.sh",
        command_windows="cmd /d /c echo hi",
        matcher=None,
        timeout_s=60.0,
        source="codex",
    )
    monkeypatch.setattr(_policy.sys, "platform", "win32")
    argv = _policy._hook_argv(spec, lambda message: None)
    assert argv[0] == "cmd"
    assert argv[-1] == "cmd /d /c echo hi"

    # On POSIX the bash-shaped command wins.
    monkeypatch.setattr(_policy.sys, "platform", "linux")
    posix = _policy._hook_argv(spec, lambda message: None)
    assert posix == ["bash", "-c", "bash payload.sh"]


def test_codex_agents_md_chain_and_import(keyless_home, tmp_path):
    """Codex Memory discovers the AGENTS.md chain and resolves its @CLAUDE.md stub."""
    codex_home = keyless_home / ".codex"
    codex_home.mkdir()
    (codex_home / "AGENTS.md").write_text("Codex global rule.", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("Imported CLAUDE stub content.", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("Workspace AGENTS rule.\n@CLAUDE.md\n", encoding="utf-8")
    ctx = HostContext.from_host(Host.CODEX)

    files = discover_memory(workspace, ctx, lambda message: None)
    contents = "\n".join(memory.content for memory in files)
    assert "Codex global rule." in contents
    assert "Workspace AGENTS rule." in contents
    # The AGENTS.md -> @CLAUDE.md stub pattern resolves on the Codex Host.
    assert "Imported CLAUDE stub content." in contents
