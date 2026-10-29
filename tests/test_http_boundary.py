"""The agent's HTTP boundary: capability adapter (the SDK always advertises {form, url}; the challenge asks for
exactly {form}) and the unreachable-server adapter. Both tested on the wire."""

import asyncio
import json

import httpx2

from http_boundary import (UNREACHABLE_CODE, UNREACHABLE_ORIGIN, FormOnlyElicitationTransport,
                           UnreachableAsErrorTransport, restrict_elicitation_to_form)

CAPS = "io.modelcontextprotocol/clientCapabilities"


def request_body(capabilities, **extra_params) -> bytes:
    params = {"name": "reservar_sala", "arguments": {"responsavel": "João"}, **extra_params,
              "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28", CAPS: capabilities,
                        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}}
    return json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}).encode()


def test_the_url_mode_is_removed_and_form_mode_is_kept():
    out = json.loads(restrict_elicitation_to_form(request_body({"elicitation": {"form": {}, "url": {}}})))
    assert out["params"]["_meta"][CAPS] == {"elicitation": {"form": {}}}


def test_everything_else_in_the_message_is_untouched():
    original = request_body({"elicitation": {"form": {}, "url": {}}}, requestState="v1.AbC_-123",
                            inputResponses={"k": {"action": "accept", "content": {"sala": "sala-fusca"}}})
    before, after = json.loads(original), json.loads(restrict_elicitation_to_form(original))
    del before["params"]["_meta"][CAPS]["elicitation"]["url"]
    assert after == before
    assert after["params"]["requestState"] == "v1.AbC_-123"
    assert after["params"]["arguments"]["responsavel"] == "João"


def test_a_message_that_already_declares_form_only_is_returned_byte_identical():
    body = request_body({"elicitation": {"form": {}}})
    assert restrict_elicitation_to_form(body) is body


def test_other_capabilities_are_not_touched():
    out = json.loads(restrict_elicitation_to_form(request_body({"elicitation": {"form": {}, "url": {}}, "roots": {}})))
    assert out["params"]["_meta"][CAPS] == {"elicitation": {"form": {}}, "roots": {}}


def test_messages_without_the_capability_or_not_json_are_returned_unchanged():
    for body in (b"", b"not json", b"[]", b"{}", json.dumps({"params": {}}).encode(),
                 json.dumps({"params": {"_meta": {CAPS: {}}}}).encode(),
                 json.dumps({"params": {"_meta": {CAPS: {"elicitation": "weird"}}}}).encode(),
                 json.dumps({"params": {"_meta": {CAPS: "weird"}}}).encode()):
        assert restrict_elicitation_to_form(body) is body


def drive(transport: FormOnlyElicitationTransport, request: httpx2.Request) -> httpx2.Response:
    return asyncio.run(transport.handle_async_request(request))


class Recorder:
    """Inner transport: the last hop before the socket. What it sees is what leaves the process."""

    def __init__(self):
        self.requests: list[httpx2.Request] = []
        self.transport = httpx2.MockTransport(self.handle)

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self.requests.append(request)
        return httpx2.Response(200, json={"ok": True})


def test_post_bodies_are_rewritten_with_a_consistent_content_length_and_headers_kept():
    recorder = Recorder()
    body = request_body({"elicitation": {"form": {}, "url": {}}})
    request = httpx2.Request("POST", "http://localhost:7301/mcp", content=body,
                             headers={"Mcp-Method": "tools/call", "Mcp-Name": "reservar_sala",
                                      "MCP-Protocol-Version": "2026-07-28", "Content-Type": "application/json"})
    response = drive(FormOnlyElicitationTransport(recorder.transport), request)
    [seen] = recorder.requests
    assert response.status_code == 200
    assert json.loads(seen.content)["params"]["_meta"][CAPS] == {"elicitation": {"form": {}}}
    assert int(seen.headers["content-length"]) == len(seen.content) < len(body)
    assert seen.headers["mcp-method"] == "tools/call" and seen.headers["mcp-name"] == "reservar_sala"
    assert seen.headers["mcp-protocol-version"] == "2026-07-28"
    assert str(seen.url) == "http://localhost:7301/mcp" and seen.method == "POST"


def test_requests_that_need_no_change_are_forwarded_as_they_are():
    recorder = Recorder()
    body = request_body({"elicitation": {"form": {}}})
    drive(FormOnlyElicitationTransport(recorder.transport), httpx2.Request("POST", "http://x/mcp", content=body))
    drive(FormOnlyElicitationTransport(recorder.transport), httpx2.Request("GET", "http://x/mcp"))
    assert [r.method for r in recorder.requests] == ["POST", "GET"]
    assert recorder.requests[0].content == body


# --- unreachable server --------------------------------------------------------------------------------------------


def failing(error: Exception) -> httpx2.MockTransport:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        raise error
    return httpx2.MockTransport(handler)


def post(body: bytes) -> httpx2.Request:
    return httpx2.Request("POST", "http://localhost:7301/mcp", content=body)


def test_a_connection_failure_becomes_a_json_rpc_error_response_that_keeps_the_request_id():
    transport = UnreachableAsErrorTransport(failing(httpx2.ConnectError("refused: 10061 secret-ish detail")))
    response = drive(transport, post(json.dumps({"jsonrpc": "2.0", "id": 41, "method": "tools/list"}).encode()))
    body = response.json()
    assert response.status_code == 503
    assert body["id"] == 41 and body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == UNREACHABLE_CODE
    assert body["error"]["data"] == {"origin": UNREACHABLE_ORIGIN, "reason": "ConnectError"}
    assert "refused" not in json.dumps(body) and "10061" not in json.dumps(body)


def test_timeouts_and_protocol_errors_are_treated_the_same_way():
    for error in (httpx2.ReadTimeout("slow"), httpx2.RemoteProtocolError("garbage")):
        response = drive(UnreachableAsErrorTransport(failing(error)), post(b'{"id": "abc"}'))
        assert response.status_code == 503 and response.json()["id"] == "abc"
        assert response.json()["error"]["data"]["reason"] == type(error).__name__


def test_a_body_that_is_not_a_json_rpc_message_still_gets_an_error_with_a_null_id():
    for body in (b"", b"not json", b"[]"):
        assert drive(UnreachableAsErrorTransport(failing(httpx2.ConnectError("x"))), post(body)).json()["id"] is None


def test_working_responses_pass_through_untouched():
    recorder = Recorder()
    response = drive(UnreachableAsErrorTransport(recorder.transport), post(b'{"id": 1}'))
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_errors_that_are_not_transport_failures_are_not_swallowed():
    import pytest
    with pytest.raises(ValueError):
        drive(UnreachableAsErrorTransport(failing(ValueError("bug"))), post(b"{}"))
