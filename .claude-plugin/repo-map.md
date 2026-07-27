# repo-map — .claude-plugin/

The plugin manifest pair Claude Code reads to discover and install this plugin.
The repo doubles as its own single-plugin marketplace, so both files live here.

- `plugin.json` — the plugin manifest: `{name: "chinamax", version, description,
  author}`. `version` is kept equal to the marketplace entry's version.
- `marketplace.json` — the marketplace manifest (`claude plugin marketplace add
  <repo>` reads `<repo>/.claude-plugin/marketplace.json`): `$schema`, `name:
  "chinamax-plugin"` (what qualifies `chinamax@chinamax-plugin`), `owner.name`,
  and a single `plugins` entry self-sourcing `"./"`.
