# src/ — conventions

Inventory lives in `./repo-map.md`.

- Src layout: the importable package is `src/chinamax/`, resolved by `[tool.setuptools.packages.find] where = ["src"]`. Nothing is importable from `src/` itself, and nothing here is on the path without the editable install (see the root `CLAUDE.md` for the env and test commands).
- Non-`.py` files must be declared in `[tool.setuptools.package-data]` or the built wheel silently omits them; the editable install used for testing hides that failure.
