"""Helpers for the MCP host tests: record what really crosses the wire, or stand in for the server."""

import asyncio
import json
from dataclasses import dataclass

import httpx2

CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"


@dataclass
class Exchange:
    """One HTTP exchange as seen by the last hop before the socket."""

    method: str
    headers: dict
    request: dict | None
    status: int | None = None
    response: dict | None = None


class RecordingTransport(httpx2.AsyncBaseTransport):
    def __init__(self, inner: httpx2.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx2.AsyncHTTPTransport()
        self.exchanges: list[Exchange] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        exchange = Exchange(request.method, {k.lower(): v for k, v in request.headers.items()},
                            json.loads(body) if body else None)
        self.exchanges.append(exchange)
        response = await self._inner.handle_async_request(request)
        content = await response.aread()
        exchange.status = response.status_code
        try:
            exchange.response = json.loads(content)
        except ValueError:
            exchange.response = None
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()

    def calls(self, method: str | None = None) -> list[Exchange]:
        return [e for e in self.exchanges if e.request and (method is None or e.request["method"] == method)]

    def methods(self) -> list[str]:
        return [e.request["method"] for e in self.calls()]


class StubMcpServer(httpx2.MockTransport):
    """A scripted MCP server, for contracts the real one would never break."""

    def __init__(self, *, tools=None, policy_text="versao: 1999-12-31\n\n- regra qualquer\n", call_result=None):
        super().__init__(self._handle)
        self.requests: list[dict] = []
        self._tools = tools if tools is not None else [self.booking_tool()]
        self._policy_text = policy_text
        self._call_result = call_result

    @staticmethod
    def booking_tool(required=("sala", "inicio", "fim", "responsavel")) -> dict:
        properties = {name: {"type": "string"} for name in required}
        return {"name": "reservar_sala", "description": "stub", "inputSchema": {
            "type": "object", "properties": properties, "required": list(required)}}

    @staticmethod
    def booked(policy="1999-12-31") -> dict:
        payload = {"reserva": "res-9999", "reservado": True, "sala": "sala-x", "inicio": "i", "fim": "f",
                   "responsavel": "o", "politica": policy, "motivo": None}
        return {"resultType": "complete", "isError": False, "structuredContent": payload,
                "content": [{"type": "text", "text": json.dumps(payload)}]}

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        document = json.loads(await request.aread())
        self.requests.append(document)
        method = document["method"]
        if method == "tools/list":
            result = {"resultType": "complete", "tools": self._tools, "ttlMs": 0, "cacheScope": "private"}
        elif method == "resources/read":
            result = {"resultType": "complete", "ttlMs": 0, "cacheScope": "private", "contents": [
                {"uri": "politica://uso", "mimeType": "text/markdown", "text": self._policy_text}]}
        else:
            result = self._call_result or self.booked()
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": document["id"], "result": result})

    def methods(self) -> list[str]:
        return [r["method"] for r in self.requests]


def run_with_host(url: str, scenario, *, transport=None, **host_options):
    """Run ``scenario(host)`` inside a started McpHost, on its own event loop."""
    from mcp_host import McpHost

    async def main():
        async with McpHost(url, http_transport=transport, **host_options) as host:
            return await scenario(host)

    return asyncio.run(main())
