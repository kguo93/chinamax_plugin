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
import select
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MESSAGES_PATH = "/v1/messages"
SCRIPT_EXHAUSTED = "FAKE_PROVIDER_SCRIPT_EXHAUSTED"

#: Upper bound on a scripted hang, so a handler thread cannot outlive the suite
#: if the Runtime never disconnects. A working watchdog gets there first.
HANG_BOUND_S = 20.0
#: How often a ping-drip fault emits its keepalive.
PING_INTERVAL_S = 0.02


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


def status_fault(
    status: int, body: str | None = None, headers: dict[str, str] | None = None
) -> dict:
    """Script an HTTP error response instead of a turn.

    Args:
        status: The HTTP status to serve.
        body: The response body, verbatim; a scripted error document by default.
        headers: Extra response headers, e.g. ``Retry-After``.

    Returns:
        A scripted fault.
    """
    if body is None:
        body = json.dumps(
            {
                "type": "error",
                "error": {"type": "scripted_error", "message": f"scripted HTTP {status}"},
            }
        )
    return {"fault": "status", "status": status, "body": body, "headers": headers or {}}


def hang_fault(blocks: list[dict] | None = None, partial_last: bool = False) -> dict:
    """Script a stream that stalls mid-turn and never reaches ``message_stop``.

    Args:
        blocks: Content blocks to serve before stalling.
        partial_last: Serve the final block half-finished — a ``tool_use`` whose
            input arrives in one chunk with no ``content_block_stop``.

    Returns:
        A scripted fault.
    """
    return {"fault": "hang", "blocks": list(blocks or []), "partial_last": partial_last}


def ping_fault() -> dict:
    """Script a stream carrying nothing but ``ping`` keepalives and SSE comments.

    A healthy proxy in front of a stalled model: bytes keep arriving, so a
    byte-level read timeout never fires and only a content-bearing reset rule
    catches the hang.
    """
    return {"fault": "ping"}


def eof_fault(blocks: list[dict] | None = None) -> dict:
    """Script a stream that closes cleanly before ``message_stop``."""
    return {"fault": "eof", "blocks": list(blocks or [])}


def stream_error_fault(error_type: str, message: str = "scripted stream error") -> dict:
    """Script an ``error`` event delivered inside an HTTP 200 stream."""
    return {"fault": "stream_error", "error_type": error_type, "message": message}


def drop_fault() -> dict:
    """Script a connection dropped without any response at all."""
    return {"fault": "drop"}


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
        #: Set before teardown so a hanging or dripping handler stops promptly.
        self.closing = threading.Event()
        self._lock = threading.Lock()
        self._cursor = 0
        #: turn index -> (reached, release): a gated turn records its request,
        #: sets ``reached``, and blocks BEFORE serving until ``release`` is set —
        #: the seam a steer test uses to write into the window between two turns.
        self._gates: dict[int, tuple[threading.Event, threading.Event]] = {}
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
        self.closing.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def gate(self, index: int) -> tuple[threading.Event, threading.Event]:
        """Arm a gate before the turn at ``index`` and return its two events.

        The handler serving that turn records the request, sets the first event
        (``reached``) and blocks until the caller sets the second (``release``).
        A test waits on ``reached``, does its work in the between-turns window
        (writing a steer), then sets ``release``.

        Args:
            index: The 0-based turn index to hold before serving.

        Returns:
            ``(reached, release)`` — wait the first, set the second.
        """
        reached, release = threading.Event(), threading.Event()
        with self._lock:
            self._gates[index] = (reached, release)
        return reached, release

    def take_turn(self, body: dict, headers: dict) -> dict | None:
        """Record a request and hand back the next scripted turn.

        Args:
            body: The decoded request body.
            headers: The request headers, lower-cased.

        Returns:
            The next scripted turn, or None once the script is exhausted.
        """
        with self._lock:
            index = self._cursor
            self.requests.append(
                {
                    "body": body,
                    "headers": headers,
                    "transcript_snapshot": self._snapshot(),
                }
            )
            gate = self._gates.get(index)
            if self._cursor >= len(self.script):
                scripted = None
            else:
                scripted = self.script[self._cursor]
                self._cursor += 1
        # Block AFTER releasing the lock so a concurrent read of `requests` (the
        # arriving request is already recorded) never waits on the gate.
        self._await_gate(gate)
        return scripted

    def _await_gate(
        self, gate: tuple[threading.Event, threading.Event] | None
    ) -> None:
        """Signal a gate is reached and block until it is released or teardown."""
        if gate is None:
            return
        reached, release = gate
        reached.set()
        while not release.wait(PING_INTERVAL_S):
            if self.closing.is_set():
                return

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
        if "fault" in scripted:
            self._serve_fault(body, scripted)
            return
        self._send_stream(body, scripted)

    def log_message(self, format: str, *args: object) -> None:
        """Silence the stdlib request log."""

    def _serve_fault(self, body: dict, fault: dict) -> None:
        """Serve one scripted fault instead of a turn."""
        kind = fault["fault"]
        if kind == "status":
            self._send_status_fault(fault)
        elif kind == "drop":
            # No response line at all: the handler simply closes the connection.
            self.close_connection = True
        elif kind == "hang":
            self._open_stream(body)
            self._send_prefix(fault["blocks"], fault["partial_last"])
            self._hang()
        elif kind == "ping":
            self._open_stream(body)
            self._drip_keepalives()
        elif kind == "eof":
            self._open_stream(body)
            self._send_prefix(fault["blocks"], False)
        elif kind == "stream_error":
            self._open_stream(body)
            self._event(
                "error",
                {
                    "type": "error",
                    "error": {"type": fault["error_type"], "message": fault["message"]},
                },
            )

    def _send_status_fault(self, fault: dict) -> None:
        payload = fault["body"].encode("utf-8")
        self.send_response(fault["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in fault["headers"].items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _hang(self) -> None:
        """Stall without writing anything further.

        Bounded so a broken watchdog fails the test instead of wedging the
        suite, and it returns as soon as the client goes away — the socket
        becoming readable is that disconnect arriving as EOF.
        """
        deadline = time.monotonic() + HANG_BOUND_S
        while time.monotonic() < deadline:
            if self._provider.closing.wait(PING_INTERVAL_S):
                return
            readable, _, _ = select.select([self.connection], [], [], 0)
            if readable:
                return

    def _drip_keepalives(self) -> None:
        """Emit ``ping`` events and SSE comments, and nothing content-bearing."""
        deadline = time.monotonic() + HANG_BOUND_S
        while time.monotonic() < deadline:
            try:
                self.wfile.write(b": keepalive\n\n")
                self._event("ping", {"type": "ping"})
            except OSError:
                return  # the client hung up
            if self._provider.closing.wait(PING_INTERVAL_S):
                return

    def _open_stream(self, body: dict) -> None:
        """Send the 200 and the ``message_start`` every scripted stream opens with."""
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

    def _send_prefix(self, blocks: list[dict], partial_last: bool) -> None:
        """Send scripted blocks, optionally leaving the last one unfinished."""
        for index, block in enumerate(blocks):
            if partial_last and index == len(blocks) - 1:
                self._send_partial_block(index, block)
            else:
                self._send_block(index, block)

    def _send_partial_block(self, index: int, block: dict) -> None:
        """Open a tool_use block and deliver only the first chunk of its input."""
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
        self._event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": _json_chunks(block["input"])[0],
                },
            },
        )

    def _send_stream(self, body: dict, scripted: dict) -> None:
        self._open_stream(body)
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
