"""Hermetic fake provider: an in-process server speaking Anthropic Messages.

The permanent test bed for the Runtime (ADR 0011). It serves ``POST /v1/messages``
as SSE from a per-test scripted turn list and records every request's body and
headers. Three wire details are SDK-enforced rather than cosmetic: every frame
carries an ``event:`` line (the SDK dispatches on the event name), ``message_start``
carries a complete ``Message``, and ``message_delta`` carries both ``delta`` and
``usage``.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MESSAGES_PATH = "/v1/messages"
SCRIPT_EXHAUSTED = "FAKE_PROVIDER_SCRIPT_EXHAUSTED"


def text_block(text: str) -> dict:
    """Build a scripted text block."""
    return {"type": "text", "text": text}


def tool_use_block(block_id: str, name: str, value: dict) -> dict:
    """Build a scripted tool_use block."""
    return {"type": "tool_use", "id": block_id, "name": name, "input": value}


def turn(blocks: list[dict], stop_reason: str | None = None) -> dict:
    """Build one scripted assistant turn.

    Args:
        blocks: The turn's content blocks.
        stop_reason: Overrides the reason derived from the blocks.

    Returns:
        A scripted turn the server can serve.
    """
    if stop_reason is None:
        stop_reason = (
            "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        )
    return {"blocks": blocks, "stop_reason": stop_reason}


class FakeProvider:
    """One test's provider endpoint, serving a scripted turn list."""

    def __init__(self, script: list[dict], snapshot_path: str | Path | None = None) -> None:
        """Bind an ephemeral port.

        Args:
            script: The turns to serve, in order.
            snapshot_path: A file whose contents are captured as each request
                arrives, so ordering against the Runtime can be asserted later.
        """
        self.script = list(script)
        self.snapshot_path = Path(snapshot_path) if snapshot_path is not None else None
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._cursor = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, provider=self))
        self._thread = threading.Thread(target=self._server.serve_forever)

    @property
    def base_url(self) -> str:
        """Return the base URL a Profile points at."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        """Start serving."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the listening socket deterministically."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def take_turn(self, body: dict, headers: dict) -> dict | None:
        """Record a request and hand back the next scripted turn.

        Args:
            body: The decoded request body.
            headers: The request headers, lower-cased.

        Returns:
            The next scripted turn, or None once the script is exhausted.
        """
        with self._lock:
            self.requests.append(
                {
                    "body": body,
                    "headers": headers,
                    "transcript_snapshot": self._snapshot(),
                }
            )
            if self._cursor >= len(self.script):
                return None
            scripted = self.script[self._cursor]
            self._cursor += 1
            return scripted

    def _snapshot(self) -> str:
        if self.snapshot_path is None or not self.snapshot_path.exists():
            return ""
        return self.snapshot_path.read_text(encoding="utf-8")


class _Handler(BaseHTTPRequestHandler):
    """Serves one request. HTTP/1.0 keeps connections from outliving the test."""

    def __init__(self, *args, provider: FakeProvider, **kwargs) -> None:
        self._provider = provider
        super().__init__(*args, **kwargs)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler dispatch name
        """Serve a scripted streaming Messages response."""
        if self.path != MESSAGES_PATH:
            self._send_error_json(404, f"unexpected path {self.path}")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        headers = {name.lower(): value for name, value in self.headers.items()}
        scripted = self._provider.take_turn(body, headers)
        if scripted is None:
            # A distinctive failure rather than a hang: the loop has no turn cap,
            # so a termination bug must fail in one turn instead of spinning.
            self._send_error_json(500, SCRIPT_EXHAUSTED)
            return
        self._send_stream(body, scripted)

    def log_message(self, format: str, *args: object) -> None:
        """Silence the stdlib request log."""

    def _send_stream(self, body: dict, scripted: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self._event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fake",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", "fake-model"),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        )
        for index, block in enumerate(scripted["blocks"]):
            self._send_block(index, block)
        self._event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": scripted["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        )
        self._event("message_stop", {"type": "message_stop"})

    def _send_block(self, index: int, block: dict) -> None:
        if block["type"] == "text":
            self._event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            self._event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": block["text"]},
                },
            )
        else:
            self._event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                },
            )
            for chunk in _json_chunks(block["input"]):
                self._event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": chunk},
                    },
                )
        self._event("content_block_stop", {"type": "content_block_stop", "index": index})

    def _event(self, name: str, data: dict) -> None:
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_error_json(self, status: int, message: str) -> None:
        payload = json.dumps(
            {"type": "error", "error": {"type": "fake_provider_error", "message": message}}
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _json_chunks(value: dict) -> list[str]:
    """Split a tool input into partial_json chunks, as a real stream would."""
    encoded = json.dumps(value)
    middle = max(1, len(encoded) // 2)
    return [encoded[:middle], encoded[middle:]]
