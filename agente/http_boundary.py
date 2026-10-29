"""The agent's HTTP boundary: two small ``httpx2`` transports wrapped around the real one.

Both exist because of how ``mcp`` 2.3.0 behaves, and both use the only public extension point left, the
``http_client`` of ``streamable_http_client`` (see docs/decisoes.md, phase 4). Neither implements or changes
protocol behaviour.

* ``FormOnlyElicitationTransport``: the SDK builds the capability ad inside ``ClientSession`` and always adds
  ``url: {}`` next to ``form: {}`` when an elicitation callback is set, and its per-request stamp overwrites any
  ``_meta`` override. The challenge requires exactly ``{"elicitation": {"form": {}}}``, and declaring URL mode
  would invite elicitations this agent cannot handle. The transport removes that one key; every other byte of
  the message is preserved.
* ``UnreachableAsErrorTransport``: a connection failure kills the SDK client's task group for good (every later
  call raises ``CancelledError``, even after the server is back). Answering the failed request with a 503
  JSON-RPC error instead makes it fail alone, so one long-lived client keeps its JSON-RPC ids unique and
  recovers by itself when the server returns.
"""

import json

import httpx2

UNREACHABLE_CODE = -32000
UNREACHABLE_ORIGIN = "agent-http-boundary"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"


def restrict_elicitation_to_form(body: bytes) -> bytes:
    """Drop ``elicitation.url`` from the request's declared capabilities. Returns ``body`` itself if nothing changes."""
    try:
        document = json.loads(body)
    except ValueError:
        return body
    capabilities = _declared_capabilities(document)
    elicitation = capabilities.get("elicitation") if capabilities is not None else None
    if not isinstance(elicitation, dict) or "url" not in elicitation:
        return body
    del elicitation["url"]
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _declared_capabilities(document: object) -> dict | None:
    params = document.get("params") if isinstance(document, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    capabilities = meta.get(CLIENT_CAPABILITIES_KEY) if isinstance(meta, dict) else None
    return capabilities if isinstance(capabilities, dict) else None


class FormOnlyElicitationTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            return await self._inner.handle_async_request(request)
        body = await request.aread()
        restricted = restrict_elicitation_to_form(body)
        if restricted is body:
            return await self._inner.handle_async_request(request)
        headers = [(name, value) for name, value in request.headers.multi_items() if name.lower() != "content-length"]
        rewritten = httpx2.Request(
            request.method, request.url, headers=headers, content=restricted, extensions=request.extensions
        )
        return await self._inner.handle_async_request(rewritten)

    async def aclose(self) -> None:
        await self._inner.aclose()


class UnreachableAsErrorTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            return await self._inner.handle_async_request(request)
        except httpx2.TransportError as error:
            body = {
                "jsonrpc": "2.0",
                "id": _request_id(await request.aread()),
                "error": {
                    "code": UNREACHABLE_CODE,
                    "message": "MCP server unreachable",
                    "data": {"origin": UNREACHABLE_ORIGIN, "reason": type(error).__name__},
                },
            }
            return httpx2.Response(503, json=body, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _request_id(body: bytes) -> object:
    try:
        document = json.loads(body)
    except ValueError:
        return None
    return document.get("id") if isinstance(document, dict) else None
