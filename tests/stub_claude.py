"""HTTP-level stub of the Claude Messages API for tests (like ``stub_ollama.py``).

Records every request (headers and JSON body) and returns canned Messages API JSON with
``usage``. Replies are chosen per section, read from the ``Section: <name>`` line of the
user message. The default reply is four well-grounded chunks (see ``good_chunks``).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKENS_IN = 1000
TOKENS_OUT = 500


class Reply:
    """What the stub does for a section: text, an HTTP error, a delay, or raw bytes."""

    def __init__(
        self,
        text: str = "",
        *,
        status: int = 200,
        delay: float = 0.0,
        raw: bytes | None = None,
        error_body: str = "",
    ) -> None:
        self.text, self.status, self.delay, self.raw, self.error_body = (
            text,
            status,
            delay,
            raw,
            error_body,
        )


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        server: _Server = self.server  # type: ignore[assignment]
        server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        content = body["messages"][0]["content"]
        match = re.match(r"Section: (.*)", content)
        section = match.group(1) if match else ""
        reply = server.reply_for(section, content)
        if reply.delay:
            time.sleep(reply.delay)
        if reply.status != 200:
            payload = reply.error_body.encode() or b'{"type":"error"}'
            self.send_response(reply.status)
        elif reply.raw is not None:
            payload = reply.raw
            self.send_response(200)
        else:
            payload = json.dumps(
                {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": reply.text}],
                    "model": body.get("model"),
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": TOKENS_IN, "output_tokens": TOKENS_OUT},
                }
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[dict[str, Any]] = []
        self.replies: dict[str, Reply] = {}
        self.default: Callable[[str, str], Reply] = lambda section, content: Reply(
            default_text(section, content)
        )

    def reply_for(self, section: str, content: str) -> Reply:
        return self.replies.get(section) or self.default(section, content)


def article_ids(content: str) -> list[int]:
    """Ids of the ``[n] title`` lines in a request's user content."""
    return [int(m) for m in re.findall(r"^\[(\d+)\] ", content, re.MULTILINE)]


def good_chunks(article_id: int, count: int = 1) -> str:
    """*count* chunks grounded in the test fixture's article *article_id* (see the tests)."""
    return " ".join(
        f"Orion Corp reported {article_id}00 widgets shipped to Lisbon. [[{article_id}]]"
        for _ in range(count)
    )


def default_text(section: str, content: str) -> str:
    ids = article_ids(content)
    return " ".join(good_chunks(i, 2) for i in ids)


class StubClaude:
    """Context manager running the stub on an ephemeral localhost port."""

    def __init__(self) -> None:
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def reply(self, section: str, reply: Reply | str) -> None:
        self._server.replies[section] = Reply(reply) if isinstance(reply, str) else reply

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1/messages"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return list(self._server.requests)

    def __enter__(self) -> StubClaude:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
