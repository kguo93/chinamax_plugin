from __future__ import annotations

import io
import json
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
    source = tmp_path / "agent.md"
    source.write_text(
        "---\nname: chinamax\ndescription: test adapter\n---\n\nLoad the canonical skill.\n",
        encoding="utf-8",
    )
    compiled = compile_codex_agent(source)
    assert "# chinamax-managed-plugin-version: 0.4.3" in compiled
    assert 'model = "gpt-5.6-terra"' in compiled
    assert 'model_reasoning_effort = "low"' in compiled
    assert "developer_instructions" in compiled


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
