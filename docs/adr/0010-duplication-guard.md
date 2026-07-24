# Duplication guard: contract language + Stop notice, no hard blocks

Claude is kept from redoing an in-flight Job's work by (1) the Bridge contract forbidding the Bridge from working itself, (2) a result-handling rule forbidding Claude-side substitute implementations when a Job fails or runs long, and (3) a non-blocking Stop-hook notice listing running Jobs. We rejected a PreToolUse hook hard-blocking Claude's own edits during write-capable Jobs: it would block legitimate parallel work on unrelated files and can misfire.

**Amended 2026-07-24**: the Bridge contract additionally forbids the Bridge from spawning any subagent — one named Bridge per dispatch, nothing beneath it (live transcripts showed a wrapper teammate re-spawning the Bridge unnamed). The result-handling rule tracks ADR 0007's amendment: envelope stripped, worker prose relayed untouched, never re-done or judged.
