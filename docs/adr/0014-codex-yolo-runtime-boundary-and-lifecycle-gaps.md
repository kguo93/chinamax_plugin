# Codex yolo boundary and detached lifecycle backstop

**Decided 2026-08-06.** Codex mutating task/setup actions require
`permission_mode=bypassPermissions`, surfaced to operators as `codex --yolo`.
Codex task dispatch uses Terra at low reasoning with no fork history. Runtime
`--read-only` remains independent and authoritative for tool-layer posture.
Codex SessionEnd returns immediately and starts a detached token-safe reaper;
orphan detection is documented as process/state based because no reliable native
Codex teammate-stop event was available in the installed CLI surface.

Setup previews a redacted, content-addressed plan, requires explicit consent
before applying, preserves TOML with `tomlkit`, backs up existing config, and
refuses unmanaged native-agent collisions without a second overwrite confirmation.
