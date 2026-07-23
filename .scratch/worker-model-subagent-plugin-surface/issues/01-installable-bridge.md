# Installable plugin with a dispatching Bridge Agent

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-surface/PRD.md
- ADRs: docs/adr/0006-single-bridge-agent-with-profiles.md, docs/adr/0003-detached-jobs-with-poll-relay-bridge.md, docs/adr/0010-duplication-guard.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

The plugin becomes real and dispatchable: the `.claude-plugin/` manifest pair — plugin.json plus marketplace.json (repo = marketplace, named `deepseek-plugin`) — so `claude plugin marketplace add` + `claude plugin install chinamax@deepseek-plugin` works; the Bridge Agent (`chinamax:chinamax`, Bash-only) with its codex-rescue-derived contract — one forwarding dispatch to the CLI seam, explicit profile required, flag mapping (--read-only, --resume/--fresh, bash-timeout override), prohibitions on doing any work itself — extended with the poll-relay loop (bounded status --wait cycles relaying concise progress) and Steer forwarding for messages received mid-run; plus the /chinamax:task command wrapping the same path.

## Acceptance criteria

- [ ] Plugin installs via marketplace add + install on a clean config; agent and command are discoverable
- [ ] Bridge contract enforces: no profile → refuse with the profile list; never edits files or inspects the repo itself; returns runtime output verbatim
- [ ] Dispatch through the Bridge path (exercised hermetically against the CLI seam + fake provider) detaches the Job and poll-relays progress until completion
- [ ] A message to the busy Bridge becomes a steer on the running Job; to a finished Bridge, a resume dispatch
- [ ] /chinamax:task maps its arguments onto the identical CLI invocation

## Blocked by

- jobs/01-durable-dispatch
