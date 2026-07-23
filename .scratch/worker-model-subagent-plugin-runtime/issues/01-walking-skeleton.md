# Runtime walking skeleton: profile-resolved loop runs one Job against the fake provider

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-runtime/PRD.md
- ADRs: docs/adr/0001-anthropic-messages-wire-format.md, docs/adr/0009-anthropic-sdk-in-dedicated-conda-env.md, docs/adr/0007-self-reported-results.md, docs/adr/0011-hermetic-fake-provider-tests.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

The thinnest complete path through the Runtime: a job spec (workspace, Profile name, prompt, write flag) is resolved against the shipped profiles plus `~/.claude/chinamax-profiles.json` override and `~/.claude/model-keys.env`, an `anthropic` SDK client is built for the Profile's base_url/model, and a streaming Messages tool-use loop runs with just two tools — bash (cwd-pinned) and report_result — appending every turn to the Job's Thread transcript (JSONL) and finishing when the model calls report_result, whose payload is captured verbatim as the structured result. Dispatch with a missing or unknown Profile fails fast. The hermetic fake provider server (Anthropic Messages wire, streaming, scripted turn sequences) is built here as the test bed for every later slice.

## Acceptance criteria

- [ ] Fake provider serves scripted streaming Messages responses (text, tool_use, message_stop) from an in-process HTTP server with per-test scripts
- [ ] Loop executes a scripted bash tool_use, returns tool_result, and terminates on report_result; payload {outcome, summary, changed_files, commands_run, tests, failures, concerns} captured verbatim
- [ ] Thread transcript JSONL on disk reconstructs the full message history after the run
- [ ] Profile resolution: 5 shipped rows; override file merges; keys read from model-keys.env by env-var name; no-profile and unknown-profile dispatches error clearly
- [ ] All tests pass keylessly via pytest in the `chinamax` conda env

## Blocked by

None - can start immediately
