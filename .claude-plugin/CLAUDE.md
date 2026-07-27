# .claude-plugin/ — conventions

Inventory lives in `./repo-map.md`.

- **Both manifests MUST live under `.claude-plugin/`, not the repo root.** `claude
  plugin marketplace add <path>` reads `<path>/.claude-plugin/marketplace.json`;
  a root-level `marketplace.json` fails the install outright. `plugin.json` sits
  beside it (the self-source `"./"` form), proven by the install smoke.
- **The marketplace `name` is `chinamax-plugin` (kebab-case), and it is what
  qualifies an install** — `chinamax@chinamax-plugin` resolves only because this
  field says `chinamax-plugin`. Never the repo's underscored directory name.
- **`plugin.json`'s `version` and the `marketplace.json` plugins entry's
  `version` move together** — keep them equal, as the Codex pair does. When the
  plugin version bumps, bump both. (`pyproject.toml` carries the Runtime package
  version independently; there is no automated link, so bump deliberately.)
- **The single `plugins` entry self-sources `"./"`** — the repo IS the plugin.
  Adding a second plugin here would change the marketplace shape and the install
  identifier; do not, without revisiting the install contract.
