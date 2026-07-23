# Full tool registry with tool-layer confinement and read-only mode

## Source context

- PRD: /home/klg2138/deepseek_plugin/.scratch/worker-model-subagent-plugin-runtime/PRD.md
- ADRs: docs/adr/0005-tool-layer-confinement.md, docs/adr/0006-single-bridge-agent-with-profiles.md
- Context: /home/klg2138/deepseek_plugin/CONTEXT.md

## What to build

Extend the walking skeleton's registry to the rich set — read_file, write_file, str_replace_edit, list_dir, grep, glob, apply_patch — with confinement enforced at the tool layer: every path argument realpath-checked inside the workspace (symlink escapes rejected), bash cwd-pinned with the operator's hard-ban denylist, per-command timeout (default 10 min, per-dispatch override) whose expiry returns to the model as a tool_result observation rather than failing the Job. Read-only Jobs omit write-class tools from the schema entirely and block write-shaped bash.

## Acceptance criteria

- [ ] Each of the seven new tools works end-to-end via scripted fake-provider turns against a real temp workspace
- [ ] Path confinement: absolute, relative-`..`, and symlink escapes all rejected with a clear tool_result error; in-workspace symlinks still usable
- [ ] Denylist blocks rm/dd/mkfs-class, git reset --hard/clean/push, sudo, shutdown-class, and curl|sh patterns with an explanatory observation
- [ ] A bash command exceeding its timeout returns a timeout observation and the loop continues to the next turn
- [ ] Read-only Job: write tools absent from the advertised schema; write-shaped bash blocked; read tools unaffected

## Blocked by

- runtime/01-walking-skeleton
