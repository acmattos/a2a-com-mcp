"""Per-request log on stderr: JSON-RPC method, id and ``traceparent``. Never logs arguments or state."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

MAX_PARSED_BODY = 1_048_576
TRACEPARENT_META_KEY = "traceparent"


class RequestLogMiddleware:
    """Pure ASGI middleware. It logs before the SDK validates, so rejected requests show up too."""

    def __init__(self, app: Any, stream: TextIO | None = None) -> None:
        self.app = app
        self._stream = stream

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        self._log(body)

        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    def _log(self, body: bytes) -> None:
        stream = self._stream or sys.stderr
        print(f"mcp-request {describe_request(body)}", file=stream, flush=True)


def describe_request(body: bytes) -> str:
    request = _parse(body)
    if request is None:
        return "method=<unparseable> id=- traceparent=-"
    params = request.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    traceparent = meta.get(TRACEPARENT_META_KEY) if isinstance(meta, dict) else None
    parts = [f"method={_text(request.get('method'))}", f"id={_text(request.get('id'))}"]
    name = params.get("name") if isinstance(params, dict) else None
    if name is not None:
        parts.append(f"name={_text(name)}")
    parts.append(f"traceparent={_text(traceparent)}")
    return " ".join(parts)


def _parse(body: bytes) -> dict | None:
    if len(body) > MAX_PARSED_BODY:
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _text(value: object) -> str:
    """One-line rendering: plain when it is a simple token, JSON-escaped otherwise."""
    if value is None:
        return "-"
    if isinstance(value, str) and value and value.isprintable() and not any(c.isspace() for c in value):
        return value
    return json.dumps(value, ensure_ascii=True)
