> IMPLEMENTER: READ EVERY FILE BELOW IN FULL BEFORE WRITING ANY CODE.
> Do not infer or fill gaps — all authoritative context is here.
> - @/home/klg2138/deepseek_plugin/CONTEXT.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0002-liveness-based-supervision.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0005-tool-layer-confinement.md
> - @/home/klg2138/deepseek_plugin/docs/adr/0006-single-bridge-agent-with-profiles.md
> - @/home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-runtime/PRD.md
> - @/home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-runtime/issues/02-tool-registry-confinement.md

# Plan — Full tool registry with tool-layer confinement

## Solution

Grow the walking skeleton's registry to the rich set — read_file, write_file, str_replace_edit, list_dir, grep, glob, apply_patch — and enforce confinement at the tool layer: realpath-checked paths, cwd-pinned bash with the operator denylist and per-command timeouts returned as observations, and a read-only mode that removes write-class tools from both the schema and the dispatch table and blocks write-shaped bash.

## Implementation Decisions

- New module `confinement.py`: `resolve_in_workspace(workspace, path)` — expands, realpaths, and requires the result be inside the workspace realpath, compared component-wise (`Path.is_relative_to`/`os.path.commonpath`, never `str.startswith`, so a sibling like `/ws-evil` is rejected against workspace `/ws`) with the workspace root itself allowed; symlinks are followed and the TARGET must be inside; violations raise a ToolError rendered as a tool_result error observation (loop continues). The workspace realpath is resolved once per Job and passed in, so per-entry revalidation during traversal costs one realpath call, not two.
- One registry dispatch boundary owns exception normalization, output truncation, and posture filtering. It catches `Exception` — decode errors on binary files, permission errors, invalid model-supplied grep regexes — and renders each as an error observation, so no tool input can kill the Job; it deliberately does NOT catch `BaseException`, leaving `KeyboardInterrupt`/`SystemExit` free for the jobs scope's cancel path. Unexpected internal failures are logged with their traceback and returned sanitized, so a Runtime bug is never laundered into a model-facing "you did something wrong". Slice 02 extracts slice 01's bash-local ~50 KB tail truncation into this boundary and applies it to every tool's output rather than inventing a second mechanism.
- Tools (each a small class in `tools/`, JSON schema + execute): read_file (1-based `offset`/`limit` like a plain numbered read), write_file (create/overwrite, mkdir -p parents inside workspace), str_replace_edit (literal — non-regex — replace requiring exactly one occurrence in the whole file; zero or multiple matches → error observation), list_dir (single level), grep (recursive fixed/regex search via Python, not shelling out), glob, apply_patch (unified diff, `a/`/`b/` prefixes stripped, `/dev/null` headers create and delete files, strict context matching with no fuzz; git rename/copy/mode-change headers are unsupported and return an error observation rather than undefined behavior).
- Traversal primitive: grep and glob walk via `os.walk(followlinks=False)`, NOT `pathlib.rglob`/`glob.glob(recursive=True)` — on the pinned Python 3.12 those FOLLOW directory symlinks (`recurse_symlinks` only lands in 3.13) and would silently defeat the symlink test below. Every discovered entry is re-validated through `resolve_in_workspace` before being opened or returned; this applies to single-level `list_dir` too, whose entries can themselves be symlinks pointing outside. A symlink named directly still resolves normally when its target is inside. grep additionally caps files scanned and matches returned and reports the cap in the observation, so a `grep -r` over a vendored tree cannot stall a loop that ADR 0002 gives no wall-clock rescue.
- apply_patch is transactional: every path is resolved and every hunk dry-run against in-memory contents first, then results are staged to temp files inside the workspace and atomically renamed into place — a rejection on file 3 of 5 leaves the workspace untouched, and only a crash mid-rename can split the commit. Dry-run alone would not deliver this; the staging step is what makes the claim true.
- bash hardening rests on ONE shared command lexer in `confinement.py`: a `shlex`-based tokenizer splits the command line into stages at shell separators (`|`, `;`, `&&`, `||`) and identifies each stage's command token, stripping leading path components (`/bin/rm` → `rm`) and wrapper prefixes (`env`, `command`). Both the denylist and the read-only write-shaped predicates consume this single lexer — never two parallel regex stacks over the same string, which is a classic divergence source. Matching is on COMMAND POSITION, not raw substrings: this is precisely what lets `grep -rn "git push" docs/` and `pytest -k test_shutdown` run, because the quoted string lexes to one non-command token. A word-boundary regex over the raw line would match inside those quotes and fail the plan's own false-positive test — do not implement it that way.
- Denylist families, checked per stage: rm/rmdir/unlink/shred, dd/mkfs/wipefs/fdisk/parted, `git reset --hard`, `git clean` with any flag cluster containing `f` (`-f`, `-fd`, `-fdx`, `--force` — a `\b`-anchored `-f` pattern would NOT match `-fd`), `git push`, forced checkouts, sudo, shutdown/reboot/poweroff, and pipe-to-shell (any stage piping into sh/bash/zsh/python, covering both `curl … | sh` and `wget … | bash`). Blocked commands return an explanatory observation and never execute. Patterns compile once at module import; the predicates are pure functions the tests call directly as well as through the bash tool.
- Explicitly NOT in the denylist, stated so its coverage is not mistaken for comprehensiveness: network egress (`curl`/`wget`/`nc` to arbitrary hosts), process signalling (`kill`/`pkill`), and quoting/substitution evasions (`\rm`, `$(…)`, `${IFS}`). Bash is cwd-pinned but not path-confined — absolute paths and `..` stay reachable from it. All of this follows ADR 0005's "documented, not defended" stance; do not expand the denylist to chase it.
- Per-command timeout: default 600 s, per-dispatch override via job spec (`bash_timeout_s`). The child starts in its OWN process group (`start_new_session=True`) so expiry kills the child's group and not the Runtime's own: SIGTERM, a 5 s grace, then SIGKILL, then drain the pipes. The observation reports that the command TIMED OUT — not a normal exit code, since a SIGKILLed child has none — plus the output captured so far, and the Job continues (ADR 0002 spirit). Residual: a descendant that calls `setsid()` itself escapes the group and survives; documented, not chased.
- Job spec extension: slice 01's `spec.py` gains optional `bash_timeout_s` — finite, positive, and NOT a `bool` (Python's `bool` subclasses `int`, so a bare `true` would otherwise sail through an `isinstance(x, (int, float))` check as a 1 s timeout); default 600 when absent. Zero, negative, non-finite, non-numeric, or boolean fails spec validation fast rather than silently killing every command. The surface scope already exposes this as the Bridge-level `bash_timeout=<s>` key mapping onto the CLI's `--bash-timeout-s` (surface/01's seam-argv decision).
- Read-only mode (`write: false` in the spec): write_file, str_replace_edit, apply_patch omitted from the advertised tools; bash additionally blocks write-shaped stages — redirection `>`/`>>` whose target is a real file (targets `/dev/null` and fd-duplication forms like `2>&1`/`>&2` stay allowed, since `grep x f 2>&1 | head` is an ordinary read), tee, mv/cp touching workspace paths in EITHER direction (moving a file out is also a write), sed -i, git commit/apply, pip install into env — conservative pattern family documented in code; read tools unaffected.
- The read-only guarantee is ADR 0005's, not the PRD's looser "provably cannot edit" phrasing: write-class tools are gone from both schema and dispatch, and write-shaped patterns are refused, while residual bash bypasses (`python -c`, heredocs, `truncate`) are accepted and documented, not chased. Do not spend implementation effort closing them.
- Registry selection lives in one place so jobs/surface scopes inherit it unchanged: a SINGLE posture-filtered registry supplies both the advertised schema and the executable dispatch table, so a tool_use naming a filtered-out or unknown tool — e.g. a `write_file` call replayed from a resumed Thread into a read-only Job — returns an error observation instead of executing. Schema omission alone is not the enforcement point.

## Acceptance Criteria (from the issue)

- [ ] Seven new tools work end-to-end via scripted fake-provider turns against a real temp workspace
- [ ] Path confinement: absolute, relative-`..`, and symlink escapes rejected with clear errors; in-workspace symlinks usable
- [ ] Denylist blocks the hard-ban families with an explanatory observation
- [ ] Bash timeout returns an observation and the loop continues
- [ ] Read-only Job: write tools absent from schema; write-shaped bash blocked; read tools unaffected

## Tracking

- [ ] confinement.py + ToolError plumbing + `Exception`-bounded normalization at dispatch
- [ ] seven tools implemented + registered
- [ ] shared shlex command lexer + denylist + timeout-as-observation
- [ ] `spec.py` accepts and bounds-checks `bash_timeout_s`
- [ ] read-only filtering applied to schema AND dispatch + write-shaped bash blocking
- [ ] Test suite below green

## Tests

- `test_tools.py::test_<tool>_happy_path` (×7) — scripted turns drive each tool and assert the concrete filesystem/result effect stated by the issue (write_file content, str_replace uniqueness, grep hits, glob matches, apply_patch application, list_dir listing, read_file contents) (issue AC bullet 1).
- `test_confinement.py::test_absolute_escape`, `test_dotdot_escape`, `test_symlink_escape` — each returns an error observation carrying `is_error: true` and the originating `tool_use_id`, and touches nothing outside; `test_inside_symlink_ok` (AC bullet 2).
- `test_confinement.py::test_sibling_prefix_rejected` — workspace `/tmp/ws`, target `/tmp/ws-evil/f`: rejected, proving the check is component-wise and not `startswith` (AC bullet 2).
- `test_confinement.py::test_recursive_symlink_not_followed` — grep, glob, AND list_dir over a workspace containing a directory symlink to an outside tree return no outside paths and read no outside content (AC bullet 2).
- `test_tools.py::test_apply_patch_all_or_nothing` — a two-file diff whose second file escapes the workspace leaves the FIRST file unmodified and returns one error observation.
- `test_tools.py::test_tool_exception_becomes_observation` — read_file on a binary/undecodable file and grep with an invalid regex each yield an error observation and the loop proceeds to the next turn.
- `test_confinement.py::test_denylist_families` — parametrized over every family the plan promises: rm/rmdir/unlink/shred, dd/mkfs/wipefs/fdisk/parted, git reset --hard, `git clean` in each of `-f`/`-fd`/`-fdx`/`--force`, git push (including a later pipeline stage, `echo x | git push`), forced checkout (`git checkout -f`), sudo, shutdown/reboot/poweroff, and both pipe-to-shell spellings (`curl … | sh`, `wget … | bash`) — command not executed, observation explains the block (AC bullet 3).
- `test_confinement.py::test_denylist_no_false_positive` — `grep -rn "git push" docs/` and `pytest -k test_shutdown` execute normally. This is the test that forces shlex command-position matching: a word-boundary regex over the raw line matches inside the quotes and fails here.
- `test_bash_timeout.py::test_timeout_observation_continues` — scripted `sleep 5` with a 1 s job-spec timeout: observation reports timeout and includes output captured so far; the NEXT scripted turn still executes; the child's process group — including a backgrounded descendant it spawned — is dead while the Runtime itself survives (AC bullet 4).
- `test_bash_timeout.py::test_invalid_timeout_rejected` — parametrized over `0`, a negative, a non-numeric string, `true` (the `bool`-is-`int` trap), and a non-finite float: each fails spec validation rather than killing every command. A spec omitting the field resolves to the documented 600 s default.
- `test_readonly.py::test_write_tools_absent_from_schema` — the recorded request's tools list lacks write_file/str_replace_edit/apply_patch (AC bullet 5).
- `test_readonly.py::test_unadvertised_write_tool_rejected_at_dispatch` — a scripted turn calls `write_file` anyway in a read-only Job: no file is created, an error observation is returned, and the loop continues. A companion case calls a tool name that was never registered at all, pinning the unknown-tool branch the plan claims (AC bullet 5).
- `test_readonly.py::test_write_shaped_bash_blocked` / `test_read_bash_allowed` — `echo x > f`, `echo x>f`, and `mv f /tmp/` blocked with observations; `ls`, `ls missing 2>/dev/null`, and `grep x f 2>&1` all execute, pinning that the redirection matcher inspects the target rather than the bare operator (AC bullet 5).

## Verification plan (run in main)

```bash
conda run -n chinamax pip install -e /home/klg2138/deepseek_plugin[test]
conda run -n chinamax python -m pytest /home/klg2138/deepseek_plugin/tests -q
```

## Out of scope

Retry/liveness ladder (slice 03); OS sandboxing (rejected, ADR 0005); job lifecycle (jobs scope); plugin surface (surface scope).
