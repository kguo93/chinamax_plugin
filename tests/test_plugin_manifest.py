"""Static validation for the Claude and Codex plugin manifests.

Static-file assertions only — the full marketplace-add + install is verified
manually on the machine (surface/01's install smoke), since this environment
cannot drive the `claude` CLI.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from chinamax import doctor

#: tests/ -> repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_ROOT / ".claude-plugin"
CODEX_MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def test_manifest_and_marketplace_valid():
    """Claude and Codex manifests agree on the shipped plugin version."""
    plugin_path = MANIFEST_DIR / "plugin.json"
    marketplace_path = MANIFEST_DIR / "marketplace.json"
    assert plugin_path.is_file()
    assert marketplace_path.is_file()
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    codex = json.loads(
        (REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex_marketplace = json.loads(
        CODEX_MARKETPLACE_PATH.read_text(encoding="utf-8")
    )
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    # The plugin name is chinamax, kebab-case.
    assert plugin["name"] == "chinamax"
    assert KEBAB.match(plugin["name"]), plugin["name"]

    # The marketplace name is chinamax-plugin — what qualifies an install as
    # `chinamax@chinamax-plugin` — and is kebab-case (never the underscored dir).
    assert marketplace["name"] == "chinamax-plugin"
    assert KEBAB.match(marketplace["name"]), marketplace["name"]

    # owner.name is present.
    assert marketplace["owner"]["name"]

    # A single plugins entry, named chinamax, self-sourcing "./".
    entries = marketplace["plugins"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "chinamax"
    assert entry["source"] == "./"

    # The marketplace entry's version equals plugin.json's, as the Codex pair does.
    assert entry["version"] == plugin["version"]
    assert codex["name"] == plugin["name"] == "chinamax"
    assert codex["version"] == plugin["version"] == "0.7.1"
    # doctor.PLUGIN_VERSION gates the managed Codex agent refresh; pin it to the
    # manifest version so it can never silently lag a release again.
    assert doctor.PLUGIN_VERSION == plugin["version"]
    assert codex["homepage"] == "https://github.com/kguo93/chinamax_plugin"
    assert codex["repository"] == "https://github.com/kguo93/chinamax_plugin"
    assert codex["license"] == "GPL-2.0"
    assert codex["interface"]["category"] == "Developer tools"
    assert codex["interface"]["capabilities"] == ["Read", "Write"]
    assert codex["hooks"] == "./hooks/codex-hooks.json"

    # The Codex-native repository marketplace is separate from the Claude
    # compatibility marketplace but exposes the same plugin identity/version.
    assert CODEX_MARKETPLACE_PATH.is_file()
    assert codex_marketplace["name"] == "chinamax-plugin"
    assert codex_marketplace["interface"]["displayName"] == "chinamax"
    codex_entries = codex_marketplace["plugins"]
    assert len(codex_entries) == 1
    codex_entry = codex_entries[0]
    assert codex_entry["name"] == "chinamax"
    assert codex_entry["source"] == {"source": "local", "path": "./"}
    assert codex_entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert codex_entry["category"] == "Developer tools"
    assert project["project"]["version"] == "0.7.1"
    dependencies = project["project"]["dependencies"]
    assert any("psutil>=7.2" in value and "sys_platform == 'darwin'" in value for value in dependencies)
    assert any("filelock>=3.20" in value and "sys_platform == 'win32'" in value for value in dependencies)
    assert (REPO_ROOT / "skills" / "chinamax-bridge" / "SKILL.md").is_file()


def test_codex_setup_skill_approve_protocol():
    """skills/chinamax-setup/SKILL.md carries the prerequisite approve-and-install
    protocol (no module reads this file, so its pins live here)."""
    text = (REPO_ROOT / "skills" / "chinamax-setup" / "SKILL.md").read_text(encoding="utf-8")
    assert '"approve"' in text  # the exact consent keyword
    assert "run_policy" in text  # dispatch by run_policy
    assert "stop-on-first-failure" in text.lower()
    # The Git Bash sentence is scoped to the seam only, not the rectification rows.
    assert "SEAM invocation through Git Bash" in text
    assert "cmd /c" in text
    # Exactly one Windows zero-state fallback block.
    assert text.count("winget install --id Git.Git") == 1
    # The dropped installer-cleanup lines never appear (operator override).
    assert 'rm "$HOME/.chinamax-miniconda.sh"' not in text
    assert 'del "%TEMP%\\chinamax-miniconda.exe"' not in text
