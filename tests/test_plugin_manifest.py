"""The plugin manifests: both live under `.claude-plugin/` and describe
`chinamax@chinamax-plugin`.

Static-file assertions only — the full marketplace-add + install is verified
manually on the machine (surface/01's install smoke), since this environment
cannot drive the `claude` CLI.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

#: tests/ -> repo root; the manifests are pinned under `.claude-plugin/`.
REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_ROOT / ".claude-plugin"
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def test_manifest_and_marketplace_valid():
    """Claude and Codex manifests agree on the shipped plugin version."""
    # Both live under `.claude-plugin/`, NOT the repo root — `marketplace add`
    # reads `<path>/.claude-plugin/marketplace.json`, and a root-level file fails.
    plugin_path = MANIFEST_DIR / "plugin.json"
    marketplace_path = MANIFEST_DIR / "marketplace.json"
    assert plugin_path.is_file()
    assert marketplace_path.is_file()
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    codex = json.loads(
        (REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
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
    assert codex["version"] == plugin["version"] == "0.4.0"
    assert project["project"]["version"] == "0.4.0"
    assert (REPO_ROOT / "skills" / "chinamax-bridge" / "SKILL.md").is_file()
