"""Shared fixtures: a keyless HOME, the fake provider, and a runnable Job.

Every test runs under a temporary HOME holding a synthetic ``model-keys.env``,
with the ambient ``ANTHROPIC_*`` variables removed — so the suite is keyless and
endpoint-clean by default rather than by opting in.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from chinamax.__main__ import main
from chinamax.transcript import read_messages
from fake_provider import FakeProvider, text_block, tool_use_block, turn

PROFILE = "deepseek"
SYNTHETIC_KEYS = {
    "DEEPSEEK_API_KEY": "sk-fake-deepseek",
    "MIMO_API_KEY": "sk-fake-mimo",
    "GLM_API_KEY": "sk-fake-glm",
    "MINIMAX_API_KEY": "sk-fake-minimax",
    "KIMI_API_KEY": "sk-fake-kimi",
}
AMBIENT_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

BASH_COMMAND = "echo hello > out.txt && cat out.txt"
BASH_TOOL_USE_ID = "toolu_bash_1"
REPORT_TOOL_USE_ID = "toolu_report_1"
REPORT_PAYLOAD = {
    "outcome": "completed",
    "summary": "Wrote out.txt in the workspace and read it back.",
    "changed_files": ["out.txt"],
    "commands_run": [BASH_COMMAND],
    "tests": ["cat out.txt"],
    "failures": [],
    "concerns": ["The workspace was empty before this job."],
}

#: Sentinel: drop this field from the generated job spec.
OMIT = object()


def bash_then_report_script() -> list[dict]:
    """Script a bash turn followed by the terminal report_result turn."""
    return [
        turn([tool_use_block(BASH_TOOL_USE_ID, "bash", {"command": BASH_COMMAND})]),
        report_turn(),
    ]


def report_turn() -> dict:
    """Script the terminal report_result turn."""
    return turn([tool_use_block(REPORT_TOOL_USE_ID, "report_result", REPORT_PAYLOAD)])


def tool_script(*calls: tuple[str, dict]) -> list[dict]:
    """Script one turn per ``(tool name, input)`` call, then the terminal turn.

    One call per turn, so a test can assert that the loop carried on to the NEXT
    turn after an observation — which is the whole point of tools that fail
    without ending the Job.
    """
    return [
        turn([tool_use_block(f"toolu_{index}", name, value)])
        for index, (name, value) in enumerate(calls)
    ] + [report_turn()]


def bash_script(*commands: str) -> list[dict]:
    """Script one bash turn per command, then the terminal turn."""
    return tool_script(*(("bash", {"command": command}) for command in commands))


def tool_results(messages: list[dict]) -> list[dict]:
    """Return every tool_result block in a message sequence, in order."""
    return [
        block
        for message in messages
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def write_overlay(home: Path, rows: list[dict]) -> None:
    """Write the user Profile overlay into a temporary HOME."""
    (home / ".claude" / "chinamax-profiles.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


def write_keys(home: Path, keys: dict[str, str]) -> None:
    """Write a bare ``NAME=value`` key file into a temporary HOME."""
    (home / ".claude" / "model-keys.env").write_text(
        "".join(f"{name}={value}\n" for name, value in keys.items()), encoding="utf-8"
    )


@dataclass
class JobEnv:
    """A dispatchable Job bound to one fake provider."""

    fake: FakeProvider
    workspace: Path
    transcript_path: Path
    result_path: Path
    spec_path: Path
    profile: str
    prompt: str

    @property
    def requests(self) -> list[dict]:
        """Return the requests the fake provider has received."""
        return self.fake.requests

    def spec(self, **overrides: object) -> dict:
        """Build a job spec, overriding or (with ``OMIT``) dropping fields."""
        fields: dict[str, object] = {
            "workspace": str(self.workspace),
            "profile": self.profile,
            "prompt": self.prompt,
            "transcript_path": str(self.transcript_path),
            "result_path": str(self.result_path),
        }
        fields.update(overrides)
        return {name: value for name, value in fields.items() if value is not OMIT}

    def run(self, spec: dict | None = None) -> int:
        """Run the Job through the CLI seam and return its exit code."""
        self.spec_path.write_text(
            json.dumps(self.spec() if spec is None else spec), encoding="utf-8"
        )
        return main(["exec", str(self.spec_path)])

    def result(self) -> dict:
        """Return the stored result, parsed."""
        return json.loads(self.result_path.read_text(encoding="utf-8"))

    def observations(self) -> list[dict]:
        """Return every tool_result the Job produced, from its durable Thread."""
        return tool_results(read_messages(self.transcript_path))

    def tree(self) -> list[str]:
        """Return every path under the workspace, relative and sorted.

        Walked with ``followlinks=False`` rather than ``rglob``, which follows
        directory symlinks on Python 3.12 and would walk out of the workspace.
        """
        found = []
        for directory, dirnames, filenames in os.walk(self.workspace, followlinks=False):
            for name in dirnames + filenames:
                found.append(os.path.relpath(os.path.join(directory, name), self.workspace))
        return sorted(found)


@pytest.fixture(autouse=True)
def keyless_home(tmp_path_factory, monkeypatch) -> Path:
    """Point HOME at a temporary dir with synthetic keys and no ambient endpoint."""
    home = tmp_path_factory.mktemp("home")
    (home / ".claude").mkdir()
    write_keys(home, SYNTHETIC_KEYS)
    monkeypatch.setenv("HOME", str(home))
    for name in AMBIENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def start_fake_provider():
    """Return a factory starting per-test fake providers, torn down afterwards."""
    started: list[FakeProvider] = []

    def _start(script: list[dict], snapshot_path: Path | None = None) -> FakeProvider:
        provider = FakeProvider(script, snapshot_path=snapshot_path)
        provider.start()
        started.append(provider)
        return provider

    yield _start
    for provider in started:
        provider.stop()


@pytest.fixture
def job_env(tmp_path, keyless_home, start_fake_provider):
    """Return a factory building a Job bound to a scripted fake provider."""

    def _make(script: list[dict], profile: str = PROFILE, prompt: str = "Do the task.") -> JobEnv:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        transcript_path = tmp_path / "job.thread.jsonl"
        provider = start_fake_provider(script, snapshot_path=transcript_path)
        # The overlay is the only endpoint seam, exactly as in production.
        write_overlay(keyless_home, [{"name": PROFILE, "base_url": provider.base_url}])
        return JobEnv(
            fake=provider,
            workspace=workspace,
            transcript_path=transcript_path,
            result_path=tmp_path / "job.result.json",
            spec_path=tmp_path / "spec.json",
            profile=profile,
            prompt=prompt,
        )

    return _make


__all__ = [
    "BASH_COMMAND",
    "BASH_TOOL_USE_ID",
    "JobEnv",
    "OMIT",
    "PROFILE",
    "REPORT_PAYLOAD",
    "REPORT_TOOL_USE_ID",
    "SYNTHETIC_KEYS",
    "bash_script",
    "bash_then_report_script",
    "report_turn",
    "text_block",
    "tool_results",
    "tool_script",
    "tool_use_block",
    "turn",
    "write_keys",
    "write_overlay",
]
