# One Host-neutral Runtime with thin Claude and Codex adapters

**Decided 2026-08-06.** Host resolution is an explicit process-boundary concern:
`--host` and `CHINAMAX_HOST` take precedence, then native plugin evidence selects
Codex or Claude. The shared Runtime owns Profiles, Jobs, state transitions,
confinement, and result fidelity. Host adapters own only native manifests,
message/tool routing, paths, lifecycle hooks, and setup behavior. `PLUGIN_*` and
`~/.codex` state never fall back to Claude paths, and Claude behavior remains
compatible with its existing plugin surface.

Rejected: maintaining two Runtime implementations or copying the Bridge contract
into each Host adapter. A canonical hidden skill is the one maintained contract;
adapters load it and add only Host-specific transport details.

## Portability amendment (0.4.3)

Platform is orthogonal to Host: both Claude and Codex adapters operate on every
supported Platform, while native data roots and lifecycle mechanisms follow the
Platform. The shared Runtime remains the only implementation of Job and Thread
semantics.

**Amended 2026-08-15 (0.5.0).** Worker Host-policy enforcement (ADR 0016) puts
Host-specific settings/Memory/MCP path knowledge into the shared Runtime, keyed on
the Job's `host`, without adding a second resolution seam. `host.py` stays the ONLY
Host-resolution seam: the loop never calls `resolve_host`. The Host flows as
`JobSpec.host` (optional in the public spec format — direct `exec` specs omit it
and `run_exec` injects the process-bound Host's value); on the worker path it comes
from the claimed record's TOP-LEVEL `host` (never the `request` block) and, at
worker start, MUST equal the process-bound HostContext's — a mismatch refuses the
claim loudly rather than reading one Host's credentials under another Host's
policy. `policy.py` derives its paths from that Host's `HostContext` (Claude:
`~/.claude` settings/memory/`.mcp.json`/`~/.claude.json`; Codex: `~/.codex`
`config.toml`/`AGENTS.md`). Claude and Codex paths stay disjoint; no cross-Host
fallback is added.
