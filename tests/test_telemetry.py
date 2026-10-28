import asyncio
import io
import json

from telemetry import RequestLogMiddleware, describe_request

TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def rpc(**fields) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **fields}).encode()


def run(middleware, scope, chunks=()):
    """Drive an ASGI app with the given body chunks; returns what the inner app received."""
    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        pass

    asyncio.run(middleware(scope, receive, send))


class Recorder:
    """Inner ASGI app that records the body it is handed."""

    def __init__(self):
        self.bodies, self.scopes = [], []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope["type"])
        if scope["type"] == "http":
            message = await receive()
            self.bodies.append(message["body"])


def test_logs_method_id_and_traceparent_on_one_line():
    line = describe_request(rpc(id=7, method="tools/list", params={"_meta": {"traceparent": TRACE}}))
    assert line == f"method=tools/list id=7 traceparent={TRACE}"


def test_tool_name_is_logged_but_arguments_are_not():
    body = rpc(id="a1", method="tools/call", params={"name": "reservar_sala", "arguments": {"responsavel": "Segredo"},
                                                     "requestState": "TOKEN", "_meta": {}})
    line = describe_request(body)
    assert "name=reservar_sala" in line and "Segredo" not in line and "TOKEN" not in line
    assert line.endswith("traceparent=-")


def test_unparseable_and_non_object_bodies_are_described_safely():
    assert describe_request(b"{nope") == "method=<unparseable> id=- traceparent=-"
    assert describe_request(b"[1, 2]") == "method=<unparseable> id=- traceparent=-"


def test_hostile_values_cannot_forge_extra_log_lines():
    body = rpc(id="x\nmcp-request method=forged", method="tools/list\r\nboom", params={"_meta": {"traceparent": "a b"}})
    line = describe_request(body)
    assert "\n" not in line and "\r" not in line


def test_middleware_logs_once_and_replays_the_exact_body():
    stream, inner = io.StringIO(), Recorder()
    body = rpc(id=1, method="tools/list", params={"_meta": {"traceparent": TRACE}})
    run(RequestLogMiddleware(inner, stream), {"type": "http", "method": "POST"}, [body[:10], body[10:]])
    assert inner.bodies == [body]
    assert stream.getvalue() == f"mcp-request method=tools/list id=1 traceparent={TRACE}\n"


def test_non_post_and_non_http_scopes_pass_through_without_logging():
    stream, inner = io.StringIO(), Recorder()
    middleware = RequestLogMiddleware(inner, stream)
    run(middleware, {"type": "lifespan"})
    run(middleware, {"type": "http", "method": "GET"}, [b""])
    assert inner.scopes == ["lifespan", "http"] and stream.getvalue() == ""
