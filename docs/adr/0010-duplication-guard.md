# Duplication guard: contract language + Stop notice, no hard blocks

Claude is kept from redoing an in-flight Job's work by (1) the Bridge contract forbidding the Bridge from working itself, (2) a result-handling rule forbidding Claude-side substitute implementations when a Job fails or runs long, and (3) a non-blocking Stop-hook notice listing running Jobs. We rejected a PreToolUse hook hard-blocking Claude's own edits during write-capable Jobs: it would block legitimate parallel work on unrelated files and can misfire.
