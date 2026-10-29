"""The A2A app with a scripted MCP server behind it: contracts the real server never breaks, and the app lifecycle.

The A2A side is still driven over HTTP (Starlette's test client speaks to the real ASGI app, lifespan included).
"""

import asyncio
import json
import secrets
import threading
import time

import httpx2
import pytest
from starlette.testclient import TestClient

from a2a_helpers import A2aClient, booking_text, new_day, state_of, task_of, text_of
from host_helpers import StubMcpServer
from mcp_host import McpHost
from agent_app import create_app

STUB_URL = "http://stub/mcp"
TEXT = booking_text("sala-x", new_day(), 9, 10)


class SpyHost(McpHost):
    """Counts how often the lifespan opens and closes the one MCP client."""

    instances: list["SpyHost"] = []

    def __init__(self, url, **options):
        super().__init__(url, **options)
        self.entered = self.exited = 0
        self.created_at_enter = None
        SpyHost.instances.append(self)

    async def __aenter__(self):
        self.entered += 1
        return await super().__aenter__()

    async def __aexit__(self, *exc_info):
        self.exited += 1
        await super().__aexit__(*exc_info)


class DelayedStub(StubMcpServer):
    def __init__(self, delay: float, **kwargs):
        super().__init__(**kwargs)
        self._delay = delay

    async def _handle(self, request):
        await asyncio.sleep(self._delay)
        return await super()._handle(request)


class Client(A2aClient):
    """Same wire client, pointed at the in-process app."""

    def __init__(self, http: TestClient) -> None:
        super().__init__("/a2a")
        self._http = http

    def rpc(self, method, params, *, traceparent=None, headers=None, request_id="auto"):
        body = {"jsonrpc": "2.0", "id": secrets.token_hex(4) if request_id == "auto" else request_id,
                "method": method, "params": params}
        sent = {"Content-Type": "application/json", **({"traceparent": traceparent} if traceparent else {}),
                **(headers or {})}
        response = self._http.post("/a2a", json=body, headers=sent)
        self.bodies.append(response.text)
        return response


def make_app(stub, host_class=McpHost):
    return create_app(mcp_url=STUB_URL, host_factory=lambda url: host_class(url, http_transport=stub))


@pytest.fixture
def run():
    """``run(stub)`` -> (client, http); the app stays up (lifespan running) until the test ends."""
    stack = []

    def start(stub, host_class=McpHost):
        http = TestClient(make_app(stub, host_class))
        http.__enter__()
        stack.append(http)
        return Client(http), http

    yield start
    for http in reversed(stack):
        http.__exit__(None, None, None)


# --- the policy version comes from the resource --------------------------------------------------------------------


def test_the_artifact_reports_the_policy_version_read_from_the_resource_not_from_the_tool(run):
    stub = StubMcpServer(policy_text="versao: 2031-05-17\n\n- qualquer\n", call_result=StubMcpServer.booked("outra"))
    client, _ = run(stub)
    task = task_of(client.send(TEXT))
    assert json.loads(text_of(task["artifacts"][0]))["politica"] == "2031-05-17"


def test_a_resource_without_a_version_line_fails_the_task_and_never_books(run):
    stub = StubMcpServer(policy_text="sem versao aqui\n")
    client, _ = run(stub)
    task = task_of(client.send(TEXT))
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "tools/call" not in stub.methods()
    assert text_of(task["status"]["message"]) == "Resposta inesperada do servidor de salas."


def test_a_server_without_the_booking_tool_fails_the_task_without_calling_anything(run):
    stub = StubMcpServer(tools=[])
    client, _ = run(stub)
    assert state_of(client.send(TEXT)) == "TASK_STATE_FAILED"
    assert stub.methods() == ["tools/list"]


# --- MCP host lifecycle --------------------------------------------------------------------------------------------


def test_the_mcp_host_is_opened_once_by_the_lifespan_and_closed_on_shutdown():
    SpyHost.instances.clear()
    app = make_app(StubMcpServer(), SpyHost)
    [host] = SpyHost.instances
    assert (host.entered, host.exited) == (0, 0), "building the app must not open anything"
    with TestClient(app) as http:
        assert (host.entered, host.exited) == (1, 0)
        client = Client(http)
        client.send(TEXT)
        client.send(TEXT)
        assert (host.entered, host.exited) == (1, 0), "requests must not open or close the MCP client"
    assert (host.entered, host.exited) == (1, 1)


def test_one_mcp_client_serves_every_task_so_jsonrpc_ids_never_repeat(run):
    stub = StubMcpServer()
    client, _ = run(stub)
    for _ in range(3):
        assert state_of(client.send(TEXT)) == "TASK_STATE_COMPLETED"
    ids = [request["id"] for request in stub.requests]
    assert len(ids) == 9 and len(set(ids)) == 9 and ids == sorted(ids)
    assert stub.methods() == ["tools/list", "resources/read", "tools/call"] * 3


def test_the_app_object_carries_no_open_connection_until_the_lifespan_starts():
    stub = StubMcpServer()
    make_app(stub)
    assert stub.requests == []


# --- MCP failures never leave the Task half done and never leak internals ----------------------------------------------


def test_an_unreachable_mcp_server_fails_the_task_with_a_plain_message_and_recovers(run):
    state = {"down": True}

    class Flaky(StubMcpServer):
        async def _handle(self, request):
            if state["down"]:
                raise httpx2.ConnectError("boom: secret-host:7301")
            return await super()._handle(request)

    client, _ = run(Flaky())
    failed = task_of(client.send(TEXT))
    assert failed["status"]["state"] == "TASK_STATE_FAILED"
    assert text_of(failed["status"]["message"]) == "Servidor de salas indisponivel. Tente novamente."
    assert "secret-host" not in json.dumps(failed) and "7301" not in json.dumps(failed)
    state["down"] = False
    assert state_of(client.send(TEXT)) == "TASK_STATE_COMPLETED"
    assert client.get(failed["id"])["result"]["status"]["state"] == "TASK_STATE_FAILED"


def test_a_jsonrpc_error_from_the_mcp_server_fails_the_task_with_only_the_code(run):
    class Rejecting(StubMcpServer):
        async def _handle(self, request):
            document = json.loads(await request.aread())
            if document["method"] == "tools/call":
                return httpx2.Response(200, json={"jsonrpc": "2.0", "id": document["id"],
                                                  "error": {"code": -32602, "message": "internal detail xyz"}})
            return await super()._handle(request)

    client, _ = run(Rejecting())
    task = task_of(client.send(TEXT))
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert text_of(task["status"]["message"]) == "O servidor de salas recusou o pedido (codigo -32602)."
    assert "xyz" not in json.dumps(task)


def test_a_tool_error_is_relayed_character_for_character(run):
    message = "Mensagem exata — com acentuação e 'aspas' e 100% de fidelidade"
    result = {"resultType": "complete", "isError": True, "content": [{"type": "text", "text": message}]}
    client, _ = run(StubMcpServer(call_result=result))
    task = task_of(client.send(TEXT))
    assert text_of(task["status"]["message"]) == message and message in [text_of(m) for m in task["history"]]


# --- concurrency and isolation ----------------------------------------------------------------------------------------


def test_overlapping_tasks_send_each_their_own_trace_to_the_mcp_server(run):
    stub = DelayedStub(0.05)
    client, http = run(stub)
    traces = [secrets.token_hex(16) for _ in range(8)]
    outcomes: dict[str, str] = {}

    def go(trace_id):
        outcomes[trace_id] = state_of(Client(http).send(TEXT, traceparent=f"00-{trace_id}-{secrets.token_hex(8)}-01"))

    threads = [threading.Thread(target=go, args=(t,)) for t in traces]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert set(outcomes.values()) == {"TASK_STATE_COMPLETED"}
    for trace_id in traces:
        seen = [r["method"] for r in stub.requests if trace_id in r["params"]["_meta"].get("traceparent", "")]
        assert seen == ["tools/list", "resources/read", "tools/call"], trace_id


def test_a_message_to_a_task_that_is_still_running_is_refused_and_does_not_disturb_it(run):
    client, http = run(DelayedStub(0.3))
    first = client.rpc("SendMessage", {"message": {"messageId": "msg-a", "role": "ROLE_USER", "parts": [{"text": TEXT}]},
                                       "configuration": {"returnImmediately": True}}).json()
    task_id = first["result"]["task"]["id"]
    assert first["result"]["task"]["status"]["state"] in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
    refused = client.send("escolha=sala-fusca", task_id=task_id)
    assert refused["error"]["code"] == -32004 and "em andamento" in refused["error"]["message"]
    for _ in range(100):
        final = client.get(task_id)["result"]
        if final["status"]["state"] not in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
            break
        time.sleep(0.05)
    assert final["status"]["state"] == "TASK_STATE_COMPLETED"
    assert [text_of(m) for m in final["history"] if m["role"] == "ROLE_USER"] == [TEXT]
