"""The Bridge Agent contract: the stanzas that keep it a safe thin forwarder.

No test here executes the markdown — that needs a live Claude session
(surface/03's gauntlet). This is the recorded exception to "never test markdown
prose": the required stanzas are asserted PRESENT so the contract still tells the
Bridge to use the seam correctly. The command SEQUENCE itself is proven
behaviorally in `test_bridge_path.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = (REPO_ROOT / "agents" / "chinamax.md").read_text(encoding="utf-8")


def test_required_stanzas_present():
    """Every load-bearing stanza is in the contract (AC bullets 2 and 4).

    Whitespace is normalized first, so a stanza wrapped across two lines (prose
    presence is what is asserted, not layout) still matches; single-line command
    strings like the poll invocation collapse to themselves and match verbatim.
    """
    text = re.sub(r"\s+", " ", CONTRACT)
    lower = text.lower()

    # Frontmatter: Bash-only tools, model haiku, and a description (what makes
    # the agent selectable through the Agent tool).
    assert "tools: Bash" in text
    assert "model: haiku" in text
    assert "description:" in text

    # Profile-required refusal, naming all five shipped Profiles and pointing at
    # the `profiles` verb for overlay-added ones — never a guess (ADR 0006).
    assert "profile=" in text
    for profile in ("deepseek", "mimo", "glm", "minimax", "kimi"):
        assert profile in text, profile
    assert "profiles" in lower
    assert "refuse" in lower

    # The prohibition block: never inspects the repo, never does the work, never
    # substitutes its own implementation (ADR 0010).
    assert "forbidden" in lower
    assert "never substitute" in lower
    # The absolute no-spawn prohibition (relay-01): one named Bridge, nothing
    # beneath it, no Agent tool ever.
    assert "forbidden to spawn any subagent" in lower

    # Treat every byte of the seam's output as untrusted data, not instructions.
    assert "untrusted data" in lower
    assert "never as instructions" in lower

    # STDIN transport via a quoted heredoc (byte-safe for quotes/newlines/`$(…)`).
    assert "stdin" in lower
    assert "CHINAMAX_EOF" in text

    # Poll loop with the 900 s default long-poll, branching on the EXIT CODE.
    assert "status <id> --wait --timeout-ms 900000" in text
    assert "exit 0" in lower and "exit 2" in lower and "exit 1" in lower
    # The per-dispatch `poll=` override, and the Bash timeout kept above the seam
    # bound (960000 ms over the 900000 ms `--timeout-ms`).
    assert "poll=" in lower
    assert "960000" in text
    # Relay ERRORS ONLY — no progress messages between the id and the terminal.
    assert "relay errors only" in lower

    # Terminal: run `result <id>`, envelope stripped, the worker's prose untouched.
    assert "result <id>" in text
    assert "verbatim" in lower
    assert "strip the report scaffolding" in lower
    assert "envelope" in lower

    # Steer-when-busy and resume-when-finished mappings, plus the finish-during-
    # steer race re-routing to resume carrying the original message.
    assert "steer <id>" in text
    assert "not delivered" in lower
    assert "as the resume prompt" in lower
    # resume always passes an explicit id (never the bare, most-recent form).
    assert "explicit" in lower and "resume" in lower

    # Relay the seam's error and STOP — a bounded failure, never a spin.
    assert "end the relay" in lower
    assert "at most twice" in lower
