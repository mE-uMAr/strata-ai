"""A stand-in for Anthropic's Messages API on the loopback interface.

The metered adapter is exercised through the real SDK against this server, so the
request the SDK actually builds is what gets checked, with no network and no key.

What it emulates, and how faithfully:

- ``POST /v1/messages`` answering with a ``message`` object and a ``usage`` block.
- **Prompt caching by prefix.** Blocks are read in render order (system, then message
  content). At each ``cache_control`` breakpoint the prefix up to and including that
  block is a cache key; a key seen before is a read, a new one a write, provided the
  prefix reaches the minimum cacheable length (512 tokens). ``input_tokens`` is the
  uncached remainder, as the real API reports it.
- Errors in the real wire shape: ``{"type": "error", "error": {"type", "message"}}``.

Token counts are ``len(text) // 4``. The point is the accounting arithmetic and the
request shape, not a tokenizer.

Behaviour per request comes from ``script``, a list consumed one entry per call (the
last repeats): ``ok``, ``refusal``, ``max_tokens``, ``429``, ``529``, ``401``,
``credit``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIN_CACHEABLE = 512


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


class FakeAnthropic:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.script = ["ok"]
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.cache: set[str] = set()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append(body)
                server.headers.append({k.lower(): v for k, v in self.headers.items()})
                status, payload = server.respond(body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()

    def _behaviour(self) -> str:
        n = len(self.requests)
        return self.script[min(n, len(self.script)) - 1]

    def respond(self, body: dict) -> tuple[int, dict]:
        mode = self._behaviour()
        errors = {"429": (429, "rate_limit_error", "Number of request tokens has exceeded your rate limit."),
                  "529": (529, "overloaded_error", "Overloaded"),
                  "401": (401, "authentication_error", "invalid x-api-key"),
                  "credit": (400, "invalid_request_error", "Your credit balance is too low to access the API.")}
        if mode in errors:
            status, kind, message = errors[mode]
            return status, {"type": "error", "error": {"type": kind, "message": message}}

        blocks = list(body.get("system") or [])
        for message in body["messages"]:
            content = message["content"]
            blocks += [{"type": "text", "text": content}] if isinstance(content, str) else content
        total = sum(tokens(b.get("text", "")) for b in blocks)
        read = written = 0
        prefix = ""
        for block in blocks:
            prefix += block.get("text", "")
            if "cache_control" in block and tokens(prefix) >= MIN_CACHEABLE:
                key = hashlib.sha256((body["model"] + prefix).encode()).hexdigest()
                if key in self.cache:
                    read, written = tokens(prefix), 0
                else:
                    self.cache.add(key)
                    written = tokens(prefix) - read
        text = self.answer
        stop = "end_turn"
        if mode == "refusal":
            text, stop = "", "refusal"
        elif mode == "max_tokens":
            text, stop = self.answer[: len(self.answer) // 2], "max_tokens"
        out = min(tokens(text) if text else 0, body["max_tokens"])
        return 200, {
            "id": f"msg_fake{len(self.requests):04d}", "type": "message", "role": "assistant",
            "model": body["model"], "content": [{"type": "text", "text": text}] if text else [],
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": total - read - written, "output_tokens": out,
                      "cache_creation_input_tokens": written, "cache_read_input_tokens": read},
        }
