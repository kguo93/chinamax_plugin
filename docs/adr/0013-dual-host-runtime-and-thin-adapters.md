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
