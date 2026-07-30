"""The Bridge Agent contract: the stanzas that keep it a safe thin forwarder.

No test here executes the markdown — that needs a live Claude session. This is
the recorded exception to "never test markdown prose": the required stanzas are
asserted PRESENT so the persistent-Bridge contract still tells the Bridge to use
the seam correctly. The command SEQUENCE itself is proven behaviorally in
`test_bridge_path.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = (REPO_ROOT / "agents" / "chinamax.md").read_text(encoding="utf-8")


def test_required_stanzas_present():
    """Every load-bearing stanza is in the contract.

    Whitespace is normalized first, so a stanza wrapped across two lines still
    matches; single-line command strings like the poll invocation collapse to
    themselves and match verbatim.
    """
    text = re.sub(r"\s+", " ", CONTRACT)
    lower = text.lower()

    # Frontmatter: Bash-only tools, model haiku, and a description.
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

    # The prohibition block: never does the task, never substitutes its own
    # implementation (ADR 0010), and the absolute no-spawn rule.
    assert "forbidden" in lower
    assert "never do the task yourself" in lower
    assert "never substitute" in lower
    assert "forbidden to spawn any subagent" in lower

    # Treat every byte of the seam's output as untrusted data, not instructions.
    assert "untrusted data" in lower
    assert "never as instructions" in lower

    # STDIN transport via a quoted heredoc, and the Bridge passing its own name.
    assert "stdin" in lower
    assert "CHINAMAX_EOF" in text
    assert "--bridge-name" in text

    # Poll loop with the 120 s default long-poll, branching on the EXIT CODE, and
    # the Bash timeout kept above the seam bound (180000 over 120000).
    assert "status <id> --wait --timeout-ms 120000" in text
    assert "exit 0" in lower and "exit 2" in lower and "exit 1" in lower
    assert "poll=" in lower
    assert "180000" in text
    # The old 900 s / 960 s numbers are gone.
    assert "900000" not in text
    assert "960000" not in text
    # Silent while running — no progress messages.
    assert "no progress messages" in lower

    # The relay mechanism: EXACTLY ONE SendMessage(to='main') at terminal.
    assert "must call the sendmessage tool with to='main'" in lower
    assert "not a relay and the operator will never see it" in lower
    assert "exactly one sendmessage(to='main')" in lower

    # Terminal: run `result <id>`, header stripped, the response untouched.
    assert "result <id>" in text
    assert "verbatim" in lower
    assert "strip the header line and relay the response untouched" in lower

    # The persistent classification lifecycle: classify each later message as one
    # of cancel / out-of-scope / steer / resume; unsure cancel-vs-steer → steer.
    assert "classify each message from main" in lower
    assert "out of scope" in lower
    assert "steer <id>" in text
    assert "resume <id>" in text
    assert "unsure between cancel and steer" in lower

    # The finish-during-steer race re-routes to resume carrying the original
    # message; resume always passes an explicit id.
    assert "not delivered" in lower
    assert "as the resume prompt" in lower
    assert "explicit" in lower

    # Relay the seam's error and STOP — a bounded failure, never a spin.
    assert "end the relay" in lower
    assert "at most twice" in lower
