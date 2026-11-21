"""HTTP-level Ollama stub for tests.

Serves canned responses on a local port and records every request, so the real
summarizer code (URL, payload, response parsing) runs unmodified.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BRIEFING_MARKER = "write a structured daily intelligence briefing"
EXTRACTION_MARKER = "extract the facts from the following news article"
CANNED_SUMMARY = "A canned two-sentence summary. It exists only for tests."
CANNED_DETAIL = "A canned detail sentence."
MALFORMED_REPLY = '{"what_happened": "cut off mid-'


UNGROUNDED_SENTENCE = "It cost $987 million."


def canned_extraction(
    title: str = "", *, is_roundup: bool = False, ungrounded: bool = False
) -> dict[str, Any]:
    """A valid extraction; ``what_happened`` carries the title so each article is distinct.

    ``ungrounded`` appends a sentence whose figure is in no article text.
    """
    what = f"Canned fact about {title}." if title else CANNED_SUMMARY
    if ungrounded:
        what = f"{what} {UNGROUNDED_SENTENCE}"
    details = (
        [f"Canned detail about {title}.", f"Canned follow-up about {title}."]
        if title
        else [CANNED_DETAIL]
    )
    return {
        "what_happened": what,
        "details": details,
        "entities": ["Example Corp"],
        "numbers": ["$1.5 million"],
        "event_date": "2020-01-02",
        "status": "happened",
        "is_roundup": is_roundup,
    }


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
            text = json.dumps(self.server.briefing or CANNED_BRIEFING)  # type: ignore[attr-defined]
        elif EXTRACTION_MARKER in body.get("prompt", ""):
            text = self.server.extraction_reply(body["prompt"])  # type: ignore[attr-defined]
        else:
            text = CANNED_SUMMARY
        self._send({"model": body.get("model"), "response": text, "done": True})


class _Server(ThreadingHTTPServer):
    """Server state: request log plus knobs for malformed and roundup extraction replies."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[dict[str, Any]] = []
        self.model = ""
        self.malformed_titles: set[str] = set()
        self.roundup_titles: set[str] = set()
        self.ungrounded_titles: set[str] = set()
        self.briefing: dict[str, Any] | None = None
        self.malformed_remaining = 0
        self._lock = threading.Lock()

    def extraction_reply(self, prompt: str) -> str:
        match = re.search(r"^Article title: (.*)$", prompt, re.MULTILINE)
        title = match.group(1) if match else ""
        with self._lock:
            if self.malformed_remaining > 0:
                self.malformed_remaining -= 1
                return MALFORMED_REPLY
        if title in self.malformed_titles:
            return MALFORMED_REPLY
        return json.dumps(
            canned_extraction(
                title,
                is_roundup=title in self.roundup_titles,
                ungrounded=title in self.ungrounded_titles,
            )
        )


class StubOllama:
    """Context manager running the stub on an ephemeral localhost port.

    Article extraction calls get a valid canned extraction. To exercise failures:
    ``malformed_titles`` makes every call for those article titles return truncated
    JSON, ``malformed_next(n)`` does so for the next *n* extraction calls, and
    ``roundup_titles`` flags those articles ``is_roundup``, and ``ungrounded_titles``
    appends a sentence with a figure absent from the article. ``briefing`` replaces the
    canned briefing reply.
    """

    def __init__(
        self,
        model: str = "gemma4:e4b",
        *,
        malformed_titles: set[str] | None = None,
        roundup_titles: set[str] | None = None,
        ungrounded_titles: set[str] | None = None,
        briefing: dict[str, Any] | None = None,
    ) -> None:
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.model = model
        self._server.malformed_titles = set(malformed_titles or ())
        self._server.roundup_titles = set(roundup_titles or ())
        self._server.ungrounded_titles = set(ungrounded_titles or ())
        self._server.briefing = briefing
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def malformed_next(self, n: int) -> None:
        """Return malformed JSON for the next *n* extraction calls."""
        with self._server._lock:
            self._server.malformed_remaining = n

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
