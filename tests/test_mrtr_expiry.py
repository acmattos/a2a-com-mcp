"""requestState expiry, with a controlled clock.

The production app (``create_app``, TTL from ``config``) runs in a thread behind a real uvicorn socket;
only the SDK's ``time`` is shifted, so the 600 s TTL is never shortened and the test never sleeps.
"""

import secrets
import socket
import threading
import time
import types

import pytest
import uvicorn
from mcp.server import request_state

from config import REQUEST_STATE_TTL_SECONDS
from conftest import McpHttpClient
from server_app import create_app
from mrtr_helpers import accept, ask, is_free, respond


@pytest.fixture
def clocked_server(monkeypatch):
    clock = types.SimpleNamespace(offset=0.0)
    real_time = time.time
    monkeypatch.setattr(request_state, "time", types.SimpleNamespace(time=lambda: real_time() + clock.offset))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(secrets.token_hex(32)), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = real_time() + 30
    while not server.started:
        assert real_time() < deadline, "in-process server did not start"
        time.sleep(0.05)
    yield McpHttpClient(f"http://127.0.0.1:{port}"), clock
    server.should_exit = True
    thread.join(timeout=15)


def test_the_token_is_still_good_just_before_the_ttl(clocked_server):
    client, clock = clocked_server
    pending = ask(client)
    clock.offset = REQUEST_STATE_TTL_SECONDS - 5
    result = respond(client, pending, accept("sala-fusca"))["result"]
    assert result["resultType"] == "complete" and result["structuredContent"]["sala"] == "sala-fusca"


def test_an_expired_token_is_rejected_with_invalid_params_and_books_nothing(clocked_server):
    client, clock = clocked_server
    pending = ask(client)
    clock.offset = REQUEST_STATE_TTL_SECONDS + 5
    status, body, _ = client.call("tools/call", {
        "name": "reservar_sala", "arguments": pending.arguments,
        "inputResponses": {pending.key: accept("sala-fusca")}, "requestState": pending.state})
    assert status == 400 and body["error"]["code"] == -32602
    assert "result" not in body
    assert is_free(client, "sala-fusca", 3) and is_free(client, "sala-mirante", 3)


def test_a_token_stays_expired_even_if_it_is_resent_later(clocked_server):
    client, clock = clocked_server
    pending = ask(client)
    clock.offset = REQUEST_STATE_TTL_SECONDS + 5
    for _ in range(2):
        assert respond(client, pending, accept("sala-mirante"))["error"]["code"] == -32602


def test_a_fresh_question_after_expiry_works_again(clocked_server):
    client, clock = clocked_server
    expired = ask(client)
    clock.offset = REQUEST_STATE_TTL_SECONDS + 5
    assert respond(client, expired, accept("sala-fusca"))["error"]["code"] == -32602
    renewed = ask(client)
    assert respond(client, renewed, accept("sala-fusca"))["result"]["structuredContent"]["sala"] == "sala-fusca"
