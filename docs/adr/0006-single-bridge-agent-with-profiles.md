# One Bridge Agent, providers as Profiles, no default

**Amended 2026-08-06.** The shared Profile model is exposed through thin Claude
and Codex adapters. Codex task names use exact underscore-safe slugs and fixed
`gpt-5.6-terra`/low/no-fork dispatch settings; Claude retains Haiku/kebab names.

**Amended 2026-08-07** (Bridge model vs Profile model). Two similarly shaped
spawn fields had grown overloaded names; this reconciles them under one
vocabulary (see `CONTEXT.md`). The **Bridge model** is the model that runs the
Bridge Agent itself — fixed per Host (Claude: Haiku; Codex: `gpt-5.6-terra` at
low reasoning, no fork), never configurable by the task prompt. The **Profile
model** is the worker model string dispatched to the Runtime: the operator's
optional `/chinamax:task model=<string>` mapped to `--model`, or the Profile's
default when omitted, pinned to the Thread. THE HARD RULE: the Bridge never
feeds its own Bridge model into the Profile dispatch or to the worker — `--model`
carries only the operator's `model=<string>`, and is omitted otherwise. The
literal CLI-consumed fields stay spelled `model` (the Claude Agent `model:`, the
Codex native-agent TOML `model =`, and the Runtime `--model`/`model=`); only the
prose labels change (these Bridge/Profile-model prose names replace the earlier
`codex_model`/`claude_model` labels).

A single named subagent (`chinamax`) serves every provider; providers are config Profiles (deepseek, mimo, glm, minimax, kimi) rather than per-provider agents. Only pro tiers exist — flash/ultraspeed variants are never offered as Profiles — and there is no default Profile: every dispatch must name one explicitly, eliminating silent model selection.

**Amended 2026-07-30**: still one agent TYPE, but Bridge instances are now named `chinamax-<profile>-<task-slug>` — the slug a short human-readable description of the task, never a random string — so concurrent persistent Bridges (ADR 0003) are addressable and distinguishable by the operator at a glance. The Profile is baked into the teammate name because one Bridge serves exactly one Thread, whose Profile is fixed at dispatch. No-default-Profile is unchanged and now bites only at `/chinamax:task`: resume inherits its Thread's Profile, and the Bridge refuses any ask to switch Profile mid-Thread (that is a new `/chinamax:task`, hence a new Bridge).

**Amended 2026-08-01**: a Profile now carries `request_extras` — a dict of extra Messages-request kwargs merged into every request verbatim (`data/profiles.json` row field, `Profile.request_extras`, unpacked LAST into the request dict in `liveness.stream_with_ladder`). Every shipped row enables its provider's reasoning at that provider's ceiling, always on: deepseek and kimi `max`, mimo `high` (its ceiling — there is no `max`), glm budget-less `{"type": "enabled"}`, minimax `{"type": "adaptive"}` (its only "on"; no effort dial exists). Reasoning is provider DATA, not code — no per-provider branch anywhere. The overlay REPLACES a Profile's dict wholesale (never a deep merge); `{}` disables reasoning for that Profile; reserved keys are rejected at overlay load with a named `ChinamaxError` — the five Runtime-built request keys (`model`, `max_tokens`, `system`, `tools`, `messages`) plus the client/transport-policy kwargs `stream`/`timeout`/`extra_headers`/`extra_query`, checked at the top level AND one level inside an `extra_body` value (the SDK merges `extra_body` into the body ROOT with precedence, so a nested collision overrides the Runtime on the wire exactly like a top-level one). Credentials therefore stay on the `api_key_env`/`model-keys.env` path only — extras cannot carry them. Shipped rows bypass `_read_overlay` (trusted package data loaded by `Profile(**row)`); the suite's exact-dict assertions in `test_profiles.py` are their guard. A resumed Job re-resolves its Profile by name — extras included, exactly as `base_url`/`model` always have — so an overlay edit to `request_extras` between Jobs applies to the resumed Job. Shapes live-verified 2026-08-01 on all five providers; see the wire consequences in ADR 0001's 2026-08-01 reasoning amendment. Rejected: per-provider code branches; a structured reasoning schema; a per-dispatch effort dial; persisting resolved extras onto Job records (re-resolution by name is the established Profile semantic).

**Amended 2026-08-03** (per-dispatch pinned model): the original decision above — "Only pro tiers exist — flash/ultraspeed variants are never offered as Profiles" — is REVERSED. Tier names ("pro", "flash", "ultraspeed") are the providers' marketing labels, not plugin concepts; a dispatch may name ANY model string its Profile's endpoint accepts, via `/chinamax:task model=<string>` → `task --model` (the spec/exec path gains the same optional `model` field). Each Profile's RESOLVED model — its shipped row, which an overlay may replace — remains its **Default model**, dispatched whenever `model=` is omitted. No-default-Profile is unchanged: a dispatch still names a Profile, and `model=` only overrides that Profile's model string.

Validity is the endpoint's alone: there is NO client-side check beyond "non-empty string" at the CLI/spec seam (the Bridge additionally refuses a `model=` TOKEN carrying whitespace or quote characters — transport safety for the argv hop, not a validity check; a direct-CLI `--model "foo bar"` still flows to the provider). An invalid string fails the Job at its FIRST non-transient provider response — every 4xx outside the transient {408, 409, 429} set already classifies PERMANENT in `liveness.classify`, so no retry ladder runs after it — and the Bridge relays the provider's error verbatim. Rejected alternatives, with reasons: a dispatch-time preflight probe (an extra live call plus a transient-network refusal mode, for a fail-fast nicety); a static per-Profile allowlist (hand-maintained, lags the providers, and contradicts "any accepted string"); a `GET /v1/models` fetch (probed 2026-08-03 — only glm and minimax serve it; deepseek, mimo and kimi 404 it).

An explicit model is PINNED to the Thread: stored as `request.model` on the Job record (`state.new_record`) and replayed verbatim by every resume (`create_resume` copies `request.model` forward). ONLY the model string is pinned — endpoint, API-key source, `request_extras` and `max_tokens` still re-resolve by Profile name on resume (a pin is NOT a Profile snapshot), so an unpinned dispatch keeps the 2026-08-01 re-resolution semantic and overlay edits flow into its resumes exactly as before. Consequence, recorded plainly: a Job pinned to an invalid model fails its first request and every resume replays the pin and fails identically — that Thread's only forward path is a fresh `/chinamax:task`. The Bridge never changes a Thread's model; a mid-Thread model-change ask is OUT-OF-SCOPE (a new `/chinamax:task`, cross-ref ADR 0003).

Extras are unchanged (pure Profile data): `RESERVED_REQUEST_KEYS` bars a `model` key in OVERLAY `request_extras`, and the shipped rows — which bypass that validation as trusted package data — are guarded by `test_profiles.py`'s exact-dict assertions, so the pin is protected by overlay validation PLUS test-guarded shipped rows, not an absolute invariant at the merge seam; `stream_with_ladder` still merges extras LAST, and an incompatible extras+model combo is judged by the endpoint like any other bad request. Bridge naming is unchanged (`chinamax-<profile>-<task-slug>`, no model slug). Surfaces: `render_job_row` renders `profile (model)` when a pin exists (status lists, reap digests and both session digests inherit it) and the single-Job `status <id>` views add an explicit `model:` detail line; `result`/relay are untouched (ADR 0007).

The H1 above is retitled from "...providers as Profiles, pro tiers only, no default" to "...providers as Profiles, no default" — the reversed "pro tiers only" clause must not survive in the title (precedent: ADR 0004's reversal rewrote its heading while the original decision survived as the quoted paragraph). The slug `0006-single-bridge-agent-with-profiles` stays truthful (one Bridge agent, providers as Profiles) — no `git mv`.

**Amended 2026-08-07** (Bridge no longer refuses model/Profile-change asks): this
ADR previously said "the Bridge refuses any ask to switch Profile mid-Thread (that is
a new `/chinamax:task`, hence a new Bridge)" and "a mid-Thread model-change ask is
OUT-OF-SCOPE (a new `/chinamax:task`, cross-ref ADR 0003)." With refusals removed
(ADR 0003, amended 2026-08-07), the Bridge no longer REFUSES such a follow-up — it
accepts it as a steer/resume on its own Thread. What is unchanged is the PIN: a
Thread's model stays pinned (`request.model`, replayed by `create_resume`) and its
Profile still re-resolves by name, so carrying a "switch model/Profile" ask into the
Thread does NOT actually change them — the worker still runs on the Thread's pinned
model and resolved Profile. Actually changing model or Profile still requires a fresh
`/chinamax:task` (a new Bridge + Thread). No-default-Profile and per-dispatch model
pinning are otherwise unchanged.

**Amended 2026-08-13** (sixth Profile: qwen). The roster gains `qwen` (Alibaba
DashScope International): base_url `https://dashscope-intl.aliyuncs.com/apps/anthropic`,
default model `qwen3.8-max`, key `QWEN_API_KEY`, `request_extras`
`{"thinking": {"type": "enabled"}}` (budget-less, glm-shaped — reasoning on at the
server's xhigh default). Endpoint, Bearer auth, model string and reasoning
live-verified 2026-08-13. The shipped Profiles are now deepseek, mimo, glm, minimax,
kimi, qwen — pure Profile data (no allow-list, no per-host registration, no code
change beyond the row). Cross-reference ADR 0001 (its 2026-08-13 endpoint/wire amendment).

**Amended 2026-08-18** (Claude Bridge model: Haiku → Sonnet). The 2026-08-07
amendment above fixed the Bridge model "per Host (Claude: Haiku; Codex:
`gpt-5.6-terra` at low reasoning, no fork)". The Claude half is overridden: the
Claude **Bridge model** is now Sonnet — live operation showed the Haiku Bridge
does not follow the contract's instructions properly, despite the stepwise,
guard-heavy contract text. The operator asked for "Sonnet at low reasoning";
the decision is MODEL-ONLY because the Claude CLI exposes no per-agent
reasoning-effort control (no `effort` frontmatter field on CLI agent
definitions, no effort parameter on the Agent call — the SDK-only `effort`
field does not reach this surface, and named teammate spawns ignore agent
frontmatter regardless, ADR 0003). Codex's `gpt-5.6-terra`/low/no-fork settings
are unchanged, so the Hosts are now asymmetric on effort by platform
limitation, not by choice. The contract copies' never-feed guard replaces
"haiku" with "sonnet"; the Bridge-model/Profile-model split, the never-feed
rule, naming, and every Profile semantic are unchanged. The spawn remains an
explicit `model: "sonnet"` override in the Agent call (`commands/task.md`),
with `agents/chinamax.md` frontmatter following suit. Cross-ref ADR 0003
(amended 2026-08-18).
