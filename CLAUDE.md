# Worker-Model Subagent Plugin — conventions

Inventory lives in `./repo-map.md`. Domain vocabulary lives in `./CONTEXT.md` — use its terms (Bridge Agent, Runtime, Job, Thread, Profile) in code, docs, and commits.

## Important things to note

- When launching the Claude Bridge, always use the Haiku **Bridge model** (the
  cheapest). The Bridge model runs the Bridge itself and is distinct from the
  **Profile model** (the worker string dispatched via `--model`); never pass the
  Bridge model into the Runtime dispatch.
- Prompt/contract text aimed at the haiku Bridge (spawn prompt, hook-injected contract) must be very explicit and stepwise — numbered, one action per rule — BUT token-lean: verbosity confuses it.
- When a decision conflicts with an existing ADR, amend that ADR in place (dated `**Amended <date>**` paragraph; quote the original when reversing; `git mv` the slug if it now contradicts the content). Never create a new ADR file for a reversal.

## How to run and test

The Runtime lives in its own conda env — never `py_automation`, never the repo's ambient python:

```bash
conda create -y -n chinamax python=3.12
conda run -n chinamax pip install -e '/home/klg2138/chinamax_plugin[test]'
conda run -n chinamax python -m pytest /home/klg2138/chinamax_plugin/tests -q
```

The editable install is what puts `chinamax` on the path; the suite imports the installed package, not a relative path. `pip install -e` leaves `src/chinamax.egg-info/` and `__pycache__/` byproducts behind — build artifacts, never committed.

## Dual-Host migration (2026-08-06)

The Runtime is shared by Claude Code and Codex. Process boundaries resolve a
Host explicitly (`--host` or `CHINAMAX_HOST`); native `PLUGIN_*` evidence wins
over Claude-compatible aliases. Claude keeps `~/.claude` and
`CLAUDE_PLUGIN_DATA`; Codex uses `~/.codex`, `PLUGIN_DATA`, and its own
`chinamax-codex` fallback. Job records carry `host`; Codex session records also
carry an ownership token. Do not add cross-Host path fallbacks.

The maintained Bridge contract is `skills/chinamax-bridge/SKILL.md`. Claude's
agent/command files and Codex's root skills are thin adapters/loaders. Codex
mutating task/setup actions require `codex --yolo` (`bypassPermissions`), while
Runtime `--read-only` remains the authoritative tool-layer posture. Codex
Bridge names are deterministic underscore-safe names using the fixed Codex
**Bridge model** `gpt-5.6-terra` at low reasoning with no fork history. The
Runtime's **Profile model** (`--model`) is a separate worker selection and never
receives the Bridge model. Codex CLI 0.146.0 and plugin 0.4.0 were
installed and live-tested in `/tmp/chinamax-codex-live`; DeepSeek is the current
hard-gate evidence, while other endpoint smokes were intentionally skipped.
The CLI still clamps the registered SessionEnd hook to 3 s and has no reliable
native teammate-stop event

## Keep Claude Code and Codex marketplace manifests synchronized

These are two different marketplace formats. Never copy one marketplace file over
the other, and never treat the Codex marketplace file as a second Claude file.

| Host | Plugin manifest | Marketplace manifest | Required marketplace shape |
| --- | --- | --- | --- |
| Claude Code | `.claude-plugin/plugin.json` | `.claude-plugin/marketplace.json` | Claude marketplace schema; plugin entry uses `"source": "./"` and carries `"version"`. |
| Codex | `.codex-plugin/plugin.json` | `.agents/plugins/marketplace.json` | Codex repository marketplace; plugin entry uses `"source": {"source": "local", "path": "./"}` and the exact `policy` block. |

Keep these exact values in the named files; do not infer them from the other
Host's schema:

- `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`: `name` is `chinamax`, `version` matches `[project].version` in `pyproject.toml`, and `author.name` is `Kevin Guo`.
- `.claude-plugin/marketplace.json`: top-level `name` is `chinamax-plugin`; its single plugin entry has `name` `chinamax`, `version` matching the three version values above, `source` `./`, `category` `Developer tools`, and both `author.name` and top-level `owner.name` set to `Kevin Guo`.
- `.agents/plugins/marketplace.json`: top-level `name` is `chinamax`, `interface.displayName` is `chinamax`; its single plugin entry has `name` `chinamax`, `source.source` `local`, `source.path` `./`, and `category` `Developer tools`. This Codex catalog does not carry a plugin version or author field; do not add Claude-only fields to it.
- Descriptions may be host-specific, but must describe the same ChinamaX plugin and must not change the plugin name or marketplace name.

The Codex plugin manifest must also keep both `homepage` and `repository` set to
`https://github.com/kguo93/chinamax_plugin`, with license `GPL-2.0`. The Claude
marketplace manifest must retain its Anthropic schema URL. The Codex marketplace
entry must keep this exact policy unless the Codex contract changes:

```json
"policy": {
  "installation": "AVAILABLE",
  "authentication": "ON_INSTALL"
}
```

When changing identity, description, or version, update these deliberately:

1. `pyproject.toml` (`[project].version`).
2. `.claude-plugin/plugin.json`.
3. `.codex-plugin/plugin.json`.
4. `.claude-plugin/marketplace.json` plugin entry, including its `version`.
5. `.agents/plugins/marketplace.json` only for its Codex-specific `interface`, `category`, `source`, or `policy` fields; it has no version field to update.

Before publishing, validate both formats from the repository root:

```bash
claude plugin validate .claude-plugin/plugin.json
claude plugin validate .claude-plugin/marketplace.json
conda run -n chinamax python -m pytest tests/test_plugin_manifest.py -q
```

For Claude Code, the canonical marketplace is GitHub. Publish the committed
revision to `kguo93/chinamax_plugin`, then refresh and inspect the installed
marketplace:

```bash
claude plugin marketplace add kguo93/chinamax_plugin
claude plugin marketplace update chinamax-plugin
claude plugin marketplace list
claude plugin install chinamax@chinamax-plugin
claude plugin list
```

For Codex, use the same GitHub repository and the Codex marketplace commands:

```bash
codex plugin marketplace add kguo93/chinamax_plugin
codex plugin marketplace upgrade
codex plugin marketplace list
codex plugin add chinamax@chinamax-plugin
codex plugin list
```

The expected installed identifier on both Hosts is
`chinamax@chinamax-plugin`. A local checkout is for development only: use the
checkout path instead of the GitHub source in the Host-specific `marketplace
add` command, and do not publish the rpi4 backup remote as the canonical source.
