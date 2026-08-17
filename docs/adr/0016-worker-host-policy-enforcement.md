# Worker-side Host-policy enforcement: Policy hooks, Memory injection, Worker MCP

- Status: Accepted
- Date: 2026-08-15
- Target release: 0.5.0

## Context

The Runtime's worker loop enforced NONE of the Host operator's policy: no
settings-file hooks fired at worker seams, no CLAUDE.md/AGENTS.md content reached
worker context, and workers had no MCP tools. A worker model dispatched through
the Bridge ran with strictly less of the Host's governance than a Claude/Codex
session would. This ADR adds three orthogonal capabilities — **Policy hooks**,
**Memory injection**, and **Worker MCP** — enforced Host-side by the shared
Runtime, keyed on the Job's `host`. All three live in the single new module
`src/chinamax/policy.py`; everything else is wiring. The glossary terms are in
`CONTEXT.md`.

The whole policy layer sits OUTSIDE the Registry's exception normalization and
must NEVER raise: a discovery/translation/synthesis/dispatch failure degrades
fail-open (allow/continue) or returns an error-flavored tool_result via the
loop's `_error_result`, logged with a stable grep-able `[policy]` prefix in the
progress log. On the record-less `exec` path (no reporter — the loop's `_report`
silently swallows) those `[policy]` lines fall back to the structured
`emit_event("warning", …)` channel.

## Decision

**Amended 2026-08-17 (0.7.0): three per-Host toggles, default OFF; the `mcp=`
per-dispatch selector removed.** Two reversals of the original decision below.

1. *Harness-parity always-on → opt-in toggles.* The original enforced all three
   capabilities on every worker Job unconditionally (the Context's premise that
   "a worker model dispatched through the Bridge ran with strictly less of the
   Host's governance than a Claude/Codex session would", closed for every Job).
   Reversed: each capability — Memory injection (§B), Policy hooks (§A), Worker
   MCP (§C) — is now an independent per-Host boolean toggle, `memory` / `hooks`
   / `mcp`, in a per-Host `settings.json` at exactly
   `HostContext.state_root / "settings.json"`, and ALL THREE DEFAULT OFF (an
   absent file or key ⇒ off; a disabled feature SKIPS its discovery/spawn
   entirely, not discover-then-drop). The upgrade regression is accepted: a
   0.5/0.6 install silently runs workers with no policy until opted in (surfaced
   by setup, status, and the README). The toggles are resolved ONCE and PINNED
   in the Job record's `request` block at dispatch — `memoryEnabled` /
   `hooksEnabled` booleans beside the existing RESOLVED `mcp` name list — so a
   resume replays the pins and never re-reads the file: the SAME staleness class
   as B.4 / C.3. A legacy pre-0.7 record with a pinned `mcp` list keeps replaying
   it; one with NO `mcp` key coerces to `[]` (off) on resume, never the
   None⇒all-discovered arm.

2. *`mcp=` per-dispatch opt-out → removed.* The original §C "Per-dispatch
   opt-out" below reads: "The operator arg `mcp=`: `mcp=none` → no servers;
   `mcp=a,b` → connect only those; absent → all discovered." Reversed
   (user-directed, for consistency across the three capabilities): the `mcp=`
   Bridge-contract token and the `--mcp` CLI flag are DELETED. The global `mcp`
   toggle is the sole control — ON connects all discovered servers, OFF connects
   none — with no per-dispatch escape hatch; `memory` and `hooks` likewise have
   no dispatch token.

*Failure polarity — the one deliberate exception to fail-open.* The never-raise,
fail-open contract still holds for the Job-runtime policy layer (`Policy.build`
and everything downstream). The lone exception is the dispatch-boundary settings
loader (`load_policy_settings`): a MALFORMED `settings.json` — unparseable JSON,
a non-object document or `policy` value, a non-boolean toggle (JSON `null`
included), an unreadable file, or a directory at the path — fails the new
dispatch/`exec` with a `PolicySettingsError` (a `ChinamaxError` subclass the CLI
boundary renders `chinamax: <msg>`, exit 1) naming the file, until fixed.
Resumes never read the file, so a malformed file breaks only NEW dispatches;
status and setup read it tolerantly (flag it, never die).

### Plugin hooks are never Policy hooks

Workers never autoload plugins: the Runtime has no skills/agents/commands/MCP-
registration surface. Plugin-registered hooks are host-session machinery with
nothing legitimate to bind to in worker context — and chinamax's own plugin hooks
are actively dangerous there (`session_end` kills the session's Jobs). Policy
hooks come from settings surfaces ONLY.

### A · Policy hooks

**Events — exactly three:** PreToolUse, PostToolUse, Stop. NO SessionStart,
SessionEnd, UserPromptSubmit (a UserPromptSubmit block on the initial prompt would
strand the Job), Notification, PreCompact, or Subagent*.

**Sources — settings only.** Claude Host: managed settings (platform-specific
harness-documented location, overridable for tests via
`CHINAMAX_MANAGED_SETTINGS`), `~/.claude/settings.json`, project
`.claude/settings.json`, project `.claude/settings.local.json`. The settings
**project root** is the nearest ancestor of the Job workspace (inclusive)
containing `.claude/` or `.git`, else the workspace itself — deliberately
diverging from the Job workspace root in nested-repo layouts. Codex Host:
`~/.codex/config.toml` `[[hooks.PreToolUse]]`/`[[hooks.PostToolUse]]`/
`[[hooks.Stop]]` tables. All hook sources (both Hosts) are discovered and parsed
ONCE at Job start; mid-Job settings edits take effect on the NEXT Job (a
staleness class shared with B.4 and C.3). An unreadable/malformed source
contributes zero hooks (logged) and cannot conceal another source's
`disableAllHooks`.

**Matcher semantics.** A hook group's `matcher` is a regex FULLY matched against
the TRANSLATED Claude-canonical tool name; a missing/empty matcher or `"*"`
matches every tool; an invalid regex skips that group (fail-open, logged).

**Tool-name translation — FULL**, so existing matchers and scripts bind:
`bash`→`Bash`; `read_file`→`Read`; `write_file`→`Write`; `str_replace_edit`→
`Edit`; `grep`→`Grep` (`glob` carries the native `include`); `glob`→`Glob`;
`list_dir`→`Glob` with a synthesized `pattern:"*"`. Unmapped native keys ride
along unchanged (matchers see supersets, never lose fields). `apply_patch`
synthesizes ONE PreToolUse/PostToolUse `Edit` event per file the patch touches
(one per file even with several hunks), with `tool_input = {file_path, patch}`
where `patch` is the RAW patch segment RE-SLICED from the original patch text by
file boundary — `parse_patch` normalizes lines and keeps no raw spans, so a lossy
re-serialization would let a hook observe bytes the worker never wrote.
`parse_patch` supplies the file list/validity only and is parsed twice (once
policy-side, once inside `ApplyPatch.execute`) on the immutable patch string —
never a second patch parser. ALL per-file PreToolUse events are evaluated BEFORE
`ApplyPatch.execute` runs; any single deny vetoes the ENTIRE patch (nothing
applied). An unparseable patch errors before any per-file event exists (zero
hooks fire); per-file PostToolUse events fire only after a successful apply.
`report_result` is never a PreToolUse — it is the Stop event. MCP tools fire
hooks under their native `mcp__<server>__<tool>` names, untranslated. A
translation/synthesis failure returns an error-flavored tool_result — the layer
never raises.

**Protocol** (harness-documented I/O): stdin JSON `{session_id: <job_id>,
transcript_path, cwd: <workspace>, hook_event_name, tool_name, tool_input,
tool_response (PostToolUse only), stop_hook_active (Stop only)}`; on the
record-less `exec` path `session_id` is the spec's `job_id` or `""`. PostToolUse
fires only after a SUCCESSFUL dispatch (`is_error` false) — errored tools skip it
(harness parity). Exit 0 → parse stdout JSON (`decision`/`reason`,
`hookSpecificOutput.permissionDecision`/`permissionDecisionReason`/
`additionalContext`). Exit 2 → deny (Stop: block), stderr text fed to the worker.
Per-hook `timeout` honored (default 60 s when unset).

**Edge semantics (fail-open).** `permissionDecision:"ask"` → **allow**, reason
logged. Hook crash (other nonzero exit) or timeout → **continue**, logged. Exit 0
with empty/unparseable stdout, or any output not parsing to a recognized
decision → allow/continue — ONE rule for every malformed shape. `disableAllHooks:
true` in ANY applicable Claude settings source → no Policy hooks for the Job
(deterministic merge: any source disables). A Windows bash-resolution failure logs
its cause distinctly from a generic hook crash.

**Stop semantics — parity, no cap.** Stop fires on the worker's VALIDATE-PASSING
`report_result` attempt (a schema-invalid `report_result` is answered with an
error before the terminal branch and fires no Stop). A block answers the
`report_result` tool_use with an error-flavored tool_result carrying the hook
reason (via `_error_result`) and continues the loop by returning `(results,
None)` — NEVER a bare user turn (an unanswered tool_use followed by a bare user
turn is a provider 400). Stop fires on the FIRST validating `report_result` block
of a turn; any additional `report_result` blocks in that same turn are answered
with error-flavored tool_results. Subsequent Stop firings carry
`stop_hook_active: true`, tracked in-memory in `run_loop` — an interrupt/resume
resets it to false (a resumed Job is a new `run_loop`; observable by hook
authors, benign). NO block limit (ADR 0002's no-caps stance holds; cancel/steer
is the remedy for a runaway hook). Cost shape: each blocked cycle re-spawns the
full Stop hook set — subprocess spawns and up-to-60 s timeouts per iteration, not
just tokens.

**Execution environment.** Hook processes run with cwd = Job workspace; env
inherited from the WORKER process — i.e. post `provider.sanitize_environment()`
(the three ambient Anthropic variables `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/
`ANTHROPIC_BASE_URL` are stripped process-wide before the loop starts; no
original-env snapshot). The chinamax variables hooks DO inherit: `CHINAMAX_HOST`,
`CLAUDE_PLUGIN_DATA`, `CHINAMAX_SESSION_ID`/`_TOKEN` (the worker env carries them),
plus `CLAUDE_PROJECT_DIR` = project root on Claude Host. Hook stdout/stderr
capture is BOUNDED, reusing the bash runner's tail-buffer drain (`tools/bash.py`).
There is NO worker-marker env var (operator explicitly declined — workers get MCP
tools instead, so cbm-gate-style guidance becomes actionable). Hook commands are
NOT lexed/confined by worker confinement (operator-authored host policy, outside
the tool boundary); they run even for `--read-only` Jobs. Windows: bash-shaped
`command` strings resolve bash via `state.windows_tool_path("bash")`; Codex
`commandWindows` strings are cmd.exe lines run via cmd.exe and NEVER routed
through the Git Bash resolver; timeout-kill via the tree-kill seams
(`state.terminate_tree`/`_terminate_tree_windows`); POSIX: process-group
spawn/kill like `run_bash`.

**Ordering.** Within an event, hooks run sequentially in source order (managed →
user → project → local; config order within a file); first deny short-circuits
the remaining hooks for that event. Full per-tool-call order: PreToolUse → (deny
ends it: error tool_result, no dispatch, no lazy Memory touch) → dispatch (the
Registry's own validation included) → PostToolUse (successful dispatches only) →
lazy Memory injection (successful results only). PreToolUse allow-with-
`additionalContext` and PostToolUse block/`additionalContext` deliver as text
blocks after that tool's tool_result in the same user turn; multiple contexts
concatenate in hook order.

**Divergence (recorded):** the worker runs ALL Codex `config.toml` hooks and
deliberately IGNORES `hooks.state` trusted_hash entries and the `[features]
hooks` flag — this exceeds the Codex CLI's own gating. Operator-authored
`config.toml` entries are the consent boundary. This is distinct from chinamax's
own setup (`doctor.py`) which enables the CLI-side `features.hooks`/trusted_hash
gate host-side.

### B · Memory injection

**Meaning:** content injection, harness parity — never "force the worker to
read".

**Discovery — full chain + imports.** Host-global file (`~/.claude/CLAUDE.md` /
`~/.codex/AGENTS.md`) + every Memory file from filesystem root down the ancestor
chain to the Job workspace (inclusive) + `CLAUDE.local.md` siblings (Claude Host)
+ recursive `@`-import resolution with cycle guard (the repo's AGENTS.md→CLAUDE.md
stub pattern works on Codex Host). EXCLUDE the Claude memory store — `MEMORY.md`
and the `memory/` directory under the Claude PROJECTS root
(`~/.claude/projects/<slug>/`), never a workspace directory that merely happens to
be named `memory/`, including when reached via imports. Import grammar: `@`-imports
resolve relative to the importing file, `~` expands, max hop depth 5;
cyclic/duplicate/unreadable imports are skipped and logged; `@` inside code
fences/spans is ignored — fence/span detection is a simple line-state scanner
(``` / `~~~` regions and inline backtick spans), NOT a full Markdown parser.
Aggregate injected Memory is BOUNDED: per-file head-truncation past a generous cap
with a logged notice (an unbounded import chain could 400 the first provider
request before the Job can recover).

**Placement.** A delimited block (per-file path headers) PREPENDED to the first
user turn — never the system prompt (exact-prefix cache constraint, ADR 0001; also
true harness behavior). It rides in the transcript like any message →
replay/resume-safe. Delimiters are a FIXED versioned sentinel pair (the block
writer and B.5's scanner share ONE format definition); any content line that
would itself match a delimiter is escaped at injection time, so no imported
payload can forge or truncate a block.

**Fresh Jobs only.** Resumed Jobs never re-inject the chain; the Thread's existing
injection stands (cheapest; stale-drift accepted). Freshness is empty seeded
history (`not messages`), NOT the `seed_transcript` flag (worker specs always set
`seed_transcript=True`).

**Nested subdirectory Memory files — lazy on first touch.** When a tool call first
resolves a path inside a workspace subdirectory whose (workspace→dir] chain has an
uninjected Memory file, its content is appended alongside that tool result. Path
triggers are SCOPED to native file tools' `path` inputs and apply_patch's per-file
paths, canonicalized through the same workspace resolver dispatch uses
(`resolve_in_workspace`); bash and MCP tool paths are out of scope (uninferable).
Only SUCCESSFUL tool results trigger a lazy injection. The already-injected set is
derived ONCE at Job start by scanning the replayed transcript for injection
blocks (works across resumes, no new durable state), then updated in place —
mirroring the `read_steer_ids` seed-once pattern. The scan reads the structured
messages from the repairing reader (`read_repaired_messages`), never raw JSONL
lines. It matches STRUCTURAL injection blocks only (exact delimiter lines at
message-block boundaries), NEVER a substring search over message content — this
repo's own test files contain the marker text, and a workspace that reads them via
a tool would otherwise poison the set. Lazy injection continues to operate during
resumed Jobs.

**No special-casing chinamax checkouts.** A workspace inside a chinamax repo
injects normally. Installed-plugin roots fall outside the workspace ancestor chain
for any ordinary workspace (an operator who points a workspace INSIDE an
installed-plugin root gets normal injection there; accepted — documented, not
coded).

**Cross-Job cache-prefix narrowing:** the injected block makes each fresh Job's
first user turn workspace-specific, so first user turns diverge per workspace
(the system prompt and tools prefix are unaffected; ADR 0001).

### C · Worker MCP

**Discovery — host parity.** Claude Host: user-scope `mcpServers` from
`~/.claude.json` + that project's `projects[<dir>].mcpServers` entry + project
`.mcp.json`; Codex Host: `config.toml [mcp_servers]`. stdio transport only in this
release. Duplicate server names: nearest scope wins (project `.mcp.json` > project
entry > user scope); an advertised-name collision after `mcp__` prefixing skips
the later duplicate and logs. Per-server `env` and `cwd` are honored; an entry
that is disabled, malformed, non-stdio, or a selected name matching no discovered
server is skipped with a `[policy]` log (fail-open class).

**Per-dispatch opt-out.** *(Reversed 2026-08-17 / 0.7.0 — see the amendment at
the top of this section: the `mcp=` token and `--mcp` flag are removed; the
per-Host `mcp` toggle is the sole control, ON ⇒ all discovered, OFF ⇒ none. The
dispatch still resolves the ON selection to a CONCRETE server-name list pinned in
`request.mcp` exactly as described below, so resumes replay exact names.)* The
operator arg `mcp=`: `mcp=none` → no servers;
`mcp=a,b` → connect only those; absent → all discovered. Threads pin their MCP
selection like a Pinned model — resumes replay it. Flows: Bridge contract token →
`--mcp` CLI flag → Job record field. Pinning is CONCRETE: the dispatcher resolves
the selector at dispatch time (absent → the discovered server-name list, `none` →
`[]`, an explicit list → itself) and stores the RESOLVED name list in the
record's `request` block (like `request.model`), so a resume replays exact names
and a server configured later never appears mid-Thread.

**Prompt-cache guarantee.** Snapshot every server's tool schemas ONCE at Job
start; deterministic server & tool ordering and stable JSON serialization so the
tools array is byte-identical across turns → provider prefix caching bills full
schemas once. Ignore MCP `listChanged`. The wire guarantee comes from building the
snapshot ONCE into a single immutable tools list reused by every request (same
object → same bytes); the canonical-JSON string (sorted server & tool names +
`json.dumps(sort_keys=True, separators=(",", ":"))`, the doctor.py precedent) is
the assertion/persistence form, NOT what is handed to the SDK (the SDK takes the
list of dicts). A resumed Job re-snapshots at ITS start: a server whose schemas
changed mid-Thread shifts the tools array across the resume boundary — accepted
staleness-class drift, same class as B.4; the same-tools resume contract holds
whenever servers are unchanged.

**Tool surface.** Advertised as `mcp__<server>__<tool>` alongside native tools;
dispatch routes an advertised MCP name to its owning server connection BEFORE
`registry.dispatch` in `_run_tool_uses` — the Registry is untouched, and routing
is an exact-name lookup in the snapshot map (full advertised string →
connection+tool), never re-splitting on `__` (server names may contain `__`).
Result normalization: concatenate the MCP result's text content blocks into the
string tool_result; `isError` → error-flavored tool_result; non-text blocks
(images/resources) render as typed placeholders; the MCP path applies the SAME
output truncation bound as native dispatch (`truncate_tail`). Policy hooks fire on
them (native names). `report_result` stays the only terminal.

**Read-only Jobs KEEP MCP tools** — outside the tool-layer posture, same class as
bash network egress (existing documented residual risk; see ADR 0005 amendment).
MCP server processes run UNSANDBOXED with full host filesystem/network capability
regardless of Job posture — distinct from "tool omitted from the model".

**Lifecycle.** stdio servers spawn INSIDE `run_loop`'s `try` so the same `finally`
that hosts `_sweep_undelivered` tears them down even when startup fails midway;
bounded connect AND per-call timeouts — a dead or silent server yields an
error-flavored tool_result, never a hung Job; a server failing to start →
proceed without it, log to the progress log. Crash-path (SIGKILL/cancel) reaping
rides `terminate_tree`'s descendant walk — stdio children stay in the worker's
tree; a server that daemonizes out of it is accepted residual. Server connects run
CONCURRENTLY at Job start, each individually bounded (turn-1 delay = max, not
sum). The official `mcp` client is asyncio-based while the loop is synchronous:
`policy.py` owns ONE background thread running a private asyncio event loop that
holds every stdio session; synchronous callers submit via `run_coroutine_threadsafe`
futures bounded by the per-call timeout. Teardown is bounded too — a hung server
close falls through to the process tree-kill seams. New dependency: the official
`mcp` Python SDK (`pyproject.toml` and `doctor.py` `DEPS`; ADR 0009 amendment).

### The synthesized-payload fidelity trade-off

`list_dir`→Glob `pattern:"*"` and apply_patch's per-file `patch` segment mean a
matcher evaluates inputs the worker never literally produced. Accepted for matcher
compatibility; recorded here as a divergence.

## Rejected / accepted trade-offs

- Fail-open edges: `ask`→allow and crash→continue mean a broken hook weakens, not
  blocks, enforcement (logged).
- Codex run-all executes hooks the Codex CLI itself would refuse (untrusted hash).
- Resume staleness: memory/hook/MCP-schema edits after a fresh Job never reach
  that Thread until a new dispatch.
- Per-turn token cost: MCP schemas ride every request; mitigated by byte-stable
  prefix caching + `mcp=` opt-out; provider billing behavior is theirs, not ours.
- A permanently-blocking Stop hook = runaway Job by design (no caps).
- No worker-marker env var: harness-assuming hooks cannot cheaply self-exclude;
  the operator's chosen mitigation is capability parity (Worker MCP).
- REJECTED in review: splitting policy into a package; restructuring the
  Registry/ToolRouter; switching transcript metadata typing; touching the
  system-prompt template.

## Cross-references

Amends ADR 0001 (tools-array byte-stability), 0002 (Stop-block, no cap), 0005 (a
second gate composes ahead of tool-layer confinement, which remains the floor a
hook allow can never lift; MCP outside the read-only posture), 0009 (`mcp`
dependency), 0013 (host settings/memory/MCP path knowledge enters the shared
Runtime keyed by Job host), 0015 (cross-platform hook/MCP process execution, Git
Bash resolution, `commandWindows` handling). Where cmd.exe execution of worker
`commandWindows` hooks contradicts 0015's recorded Windows-hooks-enter-Git-Bash
decision, 0015 carries the dated reversal (original quoted before the override).
