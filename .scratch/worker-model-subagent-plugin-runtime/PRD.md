# PRD — chinamax Runtime (provider-agnostic agent loop)

Scope: ADRs 0001, 0002, 0005, 0007, 0009, 0011. Part 1 of 3 (siblings: jobs, surface).

## Problem Statement

The operator wants to hand implementation, investigation, testing, and review work to cheap non-Claude worker models (DeepSeek, MiMo, GLM, MiniMax, Kimi) the same way they hand work to Codex — but no runtime exists that can drive those providers through an autonomous tool loop in the operator's own repository. The implement-handoff skill can only launch a whole Claude Code CLI with a swapped backend: heavyweight, unsteerable, and impossible to supervise or bound.

## Solution

A Python Runtime — the custom agent loop — that owns one Job's conversation with a provider: it speaks the provider's Anthropic-compatible Messages endpoint through the official `anthropic` SDK, exposes a rich tool set (bash, read_file, write_file, str_replace_edit, list_dir, grep, glob, apply_patch, report_result) confined to the workspace, supervises the loop by liveness rather than deadlines, appends every turn to a durable Thread transcript, and finishes by relaying the worker's `report_result` self-report verbatim.

## User Stories

1. As the operator, I want the Runtime to speak each provider's Anthropic-compatible Messages endpoint, so that every provider already proven against Claude Code works without new wire code.
2. As the operator, I want Profiles (deepseek, mimo, glm, minimax, kimi — pro tiers only) resolved from a shipped profiles file plus an optional user override file, so that adding or correcting a provider is a config edit, not a code change.
3. As the operator, I want API keys resolved from `~/.claude/model-keys.env` by env-var name, so that secrets stay in one file outside every repo.
4. As the operator, I want a dispatch that names no Profile to fail fast with a clear error, so that no model is ever silently selected.
5. As a worker model, I want dedicated read_file/write_file/str_replace_edit/list_dir/grep/glob/apply_patch tools alongside bash, so that I never have to mangle multi-line edits through heredocs.
6. As a worker model, I want a mandatory `report_result` completion tool, so that my outcome, changed files, commands run, failures, and concerns reach Claude as structure, not prose.
7. As the operator, I want every file tool to hard-reject any realpath outside the workspace (symlink-escape safe), so that a Job cannot touch files beyond the repository.
8. As the operator, I want bash to run cwd-pinned in the workspace with a denylist mirroring my global hard-bans, so that catastrophic commands are refused before execution.
9. As the operator, I want read-only Jobs to disable the write tools and block write-shaped bash, so that review/investigation dispatches provably cannot edit.
10. As the operator, I want each bash command bounded by a per-command timeout (default 10 min, per-dispatch overridable) whose expiry returns to the model as an observation, so that a stuck command never kills a Job.
11. As the operator, I want no wall-clock or turn caps anywhere in the loop, so that legitimate 70-minute-plus autonomous runs are never murdered by a deadline.
12. As the operator, I want per-API-call inactivity timeouts treated as transient failures and retried (~6 attempts, exponential backoff) along with 429/5xx/connection errors, so that a Job only fails when the provider is genuinely gone.
13. As Claude, I want the final structured result to be the worker's `report_result` verbatim, so that presentation is consistent Codex-style and cheap to render.
14. As the operator, I want every request/response turn appended to a durable Thread transcript on disk, so that resuming continues the same provider context after completion or interruption.
15. As the operator, I want the Runtime to stream responses, so that liveness is observable mid-turn and inactivity is measurable.
16. As the operator, I want the Runtime installed in a dedicated fresh conda env `chinamax` (python 3.12), so that it never contaminates or depends on my other environments.
17. As a maintainer, I want the whole loop drivable against a hermetic in-process fake provider server, so that every behavior is testable keylessly and offline.
18. As a maintainer, I want the steer queue drained at each loop-iteration boundary and injected as user messages, so that mid-run guidance lands within one turn (queue file contract owned by the jobs PRD).

## Implementation Decisions

- Language/runtime: Python 3.12 in conda env `chinamax`; dependencies: `anthropic` (SDK), `pytest` (tests). Plugin scripts invoke the env's absolute python path (ADR 0009).
- Wire: official `anthropic` SDK with per-Profile `base_url`; model strings verbatim from implement-handoff (e.g. `deepseek-v4-pro[1m]`) (ADR 0001).
- Profile resolution: shipped `profiles.json` (5 rows) merged with optional `~/.claude/chinamax-profiles.json` override; each row = name, base_url, model, api_key_env (+ optional extra params). Keys loaded from `~/.claude/model-keys.env`. No default Profile.
- Loop shape: Anthropic Messages tool-use loop — send, collect tool_use blocks, execute, return tool_result blocks; repeat until `report_result` (ADR 0007 makes it the sole terminal path besides failure/cancel).
- Tool registry (rich set): bash, read_file, write_file, str_replace_edit, list_dir, grep, glob, apply_patch, report_result. Write-class tools (write_file, str_replace_edit, apply_patch) are absent from the schema in read-only Jobs.
- Confinement (ADR 0005): realpath containment checks on every path argument (component-wise, never string-prefix); bash cwd-pinned; denylist (rm/rmdir/shred/dd/mkfs/wipefs/fdisk/parted, git reset --hard, git clean -f, git push, forced checkouts, sudo, shutdown/reboot/poweroff, curl|sh patterns); read-only Jobs additionally block write-shaped bash by pattern.
- Supervision (ADR 0002): no wall-clock/turn caps; streaming inactivity timeout per API call → transient failure; retry ladder ~6 attempts exponential backoff on inactivity/429/5xx/connection errors; bash timeout expiry → tool_result observation; Job failure only on exhausted retries (permanent provider errors — auth/4xx-class — fail fast as the ladder's zero-retry degenerate case); cancellation handled by the jobs scope.
- Thread: append-only JSONL transcript of the full Messages history per Job, written before/after every API call (completed turns only across retries — an aborted attempt appends nothing, and retry events are a separate non-replay record type), sufficient to reconstruct context for resume.
- Result: `report_result` payload {outcome: completed|blocked|failed, summary, changed_files, commands_run, tests, failures, concerns} stored on the Job record and rendered verbatim.

## Testing Decisions

- Good tests assert external behavior at the Runtime's process/CLI seam: given a scripted provider and a workspace, the loop produces these files, this transcript, this result — never internal call sequences.
- Hermetic fake provider (ADR 0011): in-process HTTP server speaking Anthropic Messages (streaming), scripted per-test turn sequences, fault injection (429, 5xx, hangs, half-streams).
- Modules under test: profile resolution, tool registry + each tool, confinement policy (path escapes, symlinks, denylist, read-only mode), retry/liveness ladder, transcript persistence, report_result capture.
- Prior art: none in-repo (greenfield); pattern source is the Codex plugin's state/job layout studied during design.
- Tests live in `tests/` per operator convention; pytest.

## Out of Scope

- Job lifecycle: detachment, state store, status/result/cancel/resume, steer queue mechanics (jobs PRD).
- Claude Code integration: agent, commands, hooks, install, live verification (surface PRD).
- OS-level sandboxing (rejected, ADR 0005); runtime audit of self-reports (rejected, ADR 0007); OpenAI chat-completions support (rejected, ADR 0001); flash/ultraspeed tiers (ADR 0006).

## Further Notes

Vocabulary per CONTEXT.md: Bridge Agent, Runtime, Job, Thread, Profile, Steer, Pro. The Runtime is deliberately ignorant of Claude Code: it reads a job spec, a workspace, and a Profile, and writes state files — everything Claude-facing lives in the sibling scopes.
