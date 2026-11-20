"""HTTP-level Ollama stub for tests.

Serves canned responses on a local port and records every request, so the real
summarizer code (URL, payload, response parsing) runs unmodified.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BRIEFING_MARKER = "write a structured daily intelligence briefing"
CANNED_SUMMARY = "A canned two-sentence summary. It exists only for tests."
CANNED_BRIEFING = {
    "segments": [
        {"topic": "Lead", "text": "Canned lead segment.", "source_article_ids": [1]},
        {"topic": "Ahead", "text": "Canned look-ahead segment.", "source_article_ids": [2]},
    ]
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # silence default stderr logging
        pass

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.server.requests.append({"method": "GET", "path": self.path, "body": None})  # type: ignore[attr-defined]
        if self.path == "/api/tags":
            self._send({"models": [{"name": self.server.model}]})  # type: ignore[attr-defined]
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({"method": "POST", "path": self.path, "body": body})  # type: ignore[attr-defined]
        if self.path != "/api/generate":
            self.send_error(404)
            return
        if BRIEFING_MARKER in body.get("prompt", ""):
            text = json.dumps(CANNED_BRIEFING)
        else:
            text = CANNED_SUMMARY
        self._send({"model": body.get("model"), "response": text, "done": True})


class StubOllama:
    """Context manager running the stub on an ephemeral localhost port."""

    def __init__(self, model: str = "gemma4:e4b") -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.requests = []  # type: ignore[attr-defined]
        self._server.model = model  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return list(self._server.requests)  # type: ignore[attr-defined]

    @property
    def generate_calls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST" and r["path"] == "/api/generate"]

    def __enter__(self) -> StubOllama:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
