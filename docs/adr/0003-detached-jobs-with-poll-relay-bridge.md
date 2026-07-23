# Every dispatch detaches; the Bridge is a poll-relay

Codex's companion defaults to a foreground blocking run and only detaches on `--background`. We instead detach every dispatch immediately into a durable Job and keep the Bridge Agent alive as a poll-relay (bounded `status --wait` calls in a loop) that forwards progress to Claude and accepts mid-run messages. This gives normal-subagent interactivity without ever tying a Job's survival to the Bridge: if the Bridge or the Claude session dies, the Job runs on and remains recoverable via `/chinamax:status` and `/chinamax:result`.
