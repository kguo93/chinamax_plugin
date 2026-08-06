# Tests run against a hermetic fake provider server

**Amended 2026-08-06.** The suite also covers Host resolution/isolation, Codex
native adapter invariants, manifest parity, and managed-agent compilation.

The pytest suite drives the real detached worker, subprocess, and file layers against an in-process HTTP server speaking the Anthropic Messages wire format with scripted tool-call turns — covering background execution, persistence, resume, cancellation, path confinement, command timeouts, API-failure injection, and session lifecycle with no API keys. Rejected: SDK-level mocks (don't exercise streaming, wire errors, or detachment) and record-replay cassettes (stale and key-dependent).
