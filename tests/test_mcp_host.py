"""Phase 4: the agent as a real MCP host, over real HTTP, against the real server process."""

import asyncio
import dataclasses
import itertools
import json
import re
import socket
from pathlib import Path

import pytest

import mcp_host
from conftest import ROOT, McpHttpClient, McpServerProcess
from host_helpers import CAPABILITIES_KEY, RecordingTransport, StubMcpServer, run_with_host
from mcp_host import (Answer, BookingRequest, ContractError, Declined, InputRequired, McpHost, McpProtocolError,
                      McpUnavailable, Reserved, ToolFailed)
from mrtr_helpers import at, is_free
from tracing import parse_traceparent

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@pytest.fixture(scope="module")
def days():
    return itertools.count(4)


def booking(day, room="sala-aquario", start=9, end=10, owner="Doc") -> BookingRequest:
    return BookingRequest(room=room, start=at(day, start), end=at(day, end), owner=owner)


def host_run(server, scenario, **options):
    recorder = RecordingTransport()
    result = run_with_host(server.url + "/mcp", scenario, transport=recorder, **options)
    return result, recorder


async def occupy(host, room, day, start=14, end=15):
    assert isinstance(await host.book(booking(day, room, start, end, "Ocupante")), Reserved)


# --- discovery, resource, wire contract ----------------------------------------------------------------------------


def test_a_booking_discovers_tools_then_reads_the_policy_then_calls_the_tool(server, days):
    day = next(days)
    outcome, wire = host_run(server, lambda host: host.book(booking(day)))
    assert wire.methods() == ["tools/list", "resources/read", "tools/call"]
    assert isinstance(outcome, Reserved)


def test_a_free_booking_returns_the_reservation_and_the_policy_read_from_the_resource(server, days):
    day = next(days)
    outcome, _ = host_run(server, lambda host: host.book(booking(day, "sala-porao", 9, 10, "Doc")))
    assert outcome.policy_version == "2026-11-01"
    reservation = outcome.reservation
    assert (reservation["sala"], reservation["responsavel"]) == ("sala-porao", "Doc")
    assert reservation["inicio"] == at(day, 9) and reservation["fim"] == at(day, 10)
    assert reservation["politica"] == "2026-11-01" and reservation["reserva"].startswith("res-")


def test_the_policy_version_is_read_from_the_resource_not_hardcoded():
    stub = StubMcpServer(policy_text="versao: 1999-12-31\n\n- qualquer regra\n")
    outcome = run_with_host("http://stub/mcp", lambda host: host.book(booking(4)), transport=stub)
    assert outcome.policy_version == "1999-12-31"
    assert stub.methods() == ["tools/list", "resources/read", "tools/call"]


def test_a_policy_without_a_version_line_is_a_contract_error_and_no_booking_is_sent():
    stub = StubMcpServer(policy_text="sem versao aqui\n")
    with pytest.raises(ContractError, match="versao"):
        run_with_host("http://stub/mcp", lambda host: host.book(booking(4)), transport=stub)
    assert "tools/call" not in stub.methods()


def test_a_server_without_the_booking_tool_is_a_contract_error_and_no_call_is_sent():
    stub = StubMcpServer(tools=[{"name": "outra", "inputSchema": {"type": "object"}}])
    with pytest.raises(ContractError, match="reservar_sala"):
        run_with_host("http://stub/mcp", lambda host: host.book(booking(4)), transport=stub)
    assert stub.methods() == ["tools/list"]


def test_the_discovered_schema_is_honoured_a_new_required_argument_stops_the_call():
    stub = StubMcpServer(tools=[StubMcpServer.booking_tool(("sala", "inicio", "fim", "responsavel", "andar"))])
    with pytest.raises(ContractError, match="andar"):
        run_with_host("http://stub/mcp", lambda host: host.book(booking(4)), transport=stub)
    assert "tools/call" not in stub.methods()


def test_every_request_carries_complete_meta_and_coherent_mirrored_headers(server, days):
    day = next(days)
    occupy_day = day

    async def scenario(host):
        await occupy(host, "sala-garagem", occupy_day)
        pending = await host.book(booking(occupy_day, "sala-garagem", 14, 15, "Marty"))
        await host.resume(pending, Answer.accept("sala-fusca"))

    _, wire = host_run(server, scenario)
    assert wire.methods() == ["tools/list", "resources/read", "tools/call"] * 2 + ["tools/call"]
    for exchange in wire.calls():
        rpc, headers = exchange.request, exchange.headers
        meta = rpc["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert meta[CAPABILITIES_KEY] == {"elicitation": {"form": {}}}
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "agente-central-de-salas"
        assert headers["mcp-protocol-version"] == "2026-07-28"
        assert headers["mcp-method"] == rpc["method"]
        if rpc["method"] == "tools/call":
            assert headers["mcp-name"] == rpc["params"]["name"] == "reservar_sala"
        elif rpc["method"] == "resources/read":
            assert headers["mcp-name"] == rpc["params"]["uri"] == "politica://uso"
        else:
            assert "mcp-name" not in headers
        assert exchange.method == "POST" and exchange.status == 200


def test_no_request_is_ever_sent_without_the_full_meta_even_with_no_traceparent(server, days):
    _, wire = host_run(server, lambda host: host.book(booking(next(days))))
    assert all("traceparent" not in e.request["params"]["_meta"] for e in wire.calls())


# --- execution errors ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("request_, message", [
    (BookingRequest("sala-delorean", at(4, 9), at(4, 10), "Doc"), "Sala inexistente: sala-delorean"),
    (BookingRequest("sala-aquario", at(4, 7), at(4, 8), "Doc"),
     "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"),
    (BookingRequest("sala-aquario", at(4, 9), at(4, 12), "Doc"),
     "Duracao acima do limite: a politica permite no maximo 2 horas"),
    (BookingRequest("sala-aquario", at(4, 10), at(4, 9), "Doc"), "Intervalo invalido: fim deve ser posterior a inicio"),
])
def test_execution_errors_come_back_as_a_failure_with_the_exact_message(server, request_, message):
    outcome, _ = host_run(server, lambda host: host.book(request_))
    assert isinstance(outcome, ToolFailed) and outcome.message == message


def test_a_conflict_without_alternatives_is_a_failure_not_a_pause(server, days):
    day = next(days)

    async def scenario(host):
        for room in ("sala-garagem", "sala-fusca", "sala-mirante"):
            await occupy(host, room, day)
        return await host.book(booking(day, "sala-garagem", 14, 15, "Marty"))

    outcome, _ = host_run(server, scenario)
    assert isinstance(outcome, ToolFailed) and outcome.message == "Sem alternativas disponiveis no intervalo"


# --- the pause: raw input_required, nothing answered by the host -------------------------------------------------------


def test_a_conflict_pauses_with_the_raw_question_and_the_host_answers_nothing(server, days, monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_host, "_refuse_elicitation", lambda *a, **k: calls.append(a))
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        return await host.book(booking(day, "sala-garagem", 14, 15, "Marty"))

    outcome, wire = host_run(server, scenario)
    assert isinstance(outcome, InputRequired)
    assert outcome.choices == ("sala-fusca", "sala-mirante")
    assert outcome.message == "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
    assert len(wire.calls("tools/call")) == 2  # the occupying call and the paused one: no automatic answer
    assert calls == []
    assert is_free(McpHttpClient(server.url), "sala-fusca", day)


def test_the_pause_keeps_the_servers_key_and_state_exactly_as_received(server, days):
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        return await host.book(booking(day, "sala-garagem", 14, 15, "Marty"))

    outcome, wire = host_run(server, scenario)
    raw = wire.calls("tools/call")[-1].response["result"]
    [(key, request)] = raw["inputRequests"].items()
    assert outcome.key == key and outcome.request_state == raw["requestState"]
    assert outcome.input_requests == {key: request}
    assert outcome.request == booking(day, "sala-garagem", 14, 15, "Marty")
    assert outcome.policy_version == "2026-11-01"


def test_the_opaque_state_does_not_leak_through_repr_or_str(server, days):
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        return await host.book(booking(day, "sala-garagem", 14, 15, "Marty"))

    outcome, _ = host_run(server, scenario)
    for text in (repr(outcome), str(outcome)):
        assert outcome.request_state not in text and outcome.request_state[:30] not in text


def test_a_malformed_question_is_a_contract_error_the_host_does_not_guess():
    def asks(requests_):
        return {"resultType": "input_required", "inputRequests": requests_, "requestState": "opaque"}

    form = {"method": "elicitation/create", "params": {"mode": "form", "message": "m", "requestedSchema": {
        "type": "object", "properties": {"sala": {"type": "string", "enum": ["a"]}}, "required": ["sala"]}}}
    url_mode = {"method": "elicitation/create", "params": {"mode": "url", "message": "m", "url": "http://x",
                                                           "elicitationId": "1"}}
    for requests_ in ({}, {"a": form, "b": form}, {"a": url_mode}):
        stub = StubMcpServer(call_result=asks(requests_))
        with pytest.raises(ContractError):
            run_with_host("http://stub/mcp", lambda host: host.book(booking(4)), transport=stub)


def test_a_single_alternative_is_read_whether_the_schema_uses_enum_or_const():
    def pause(field):
        return {"resultType": "input_required", "requestState": "opaque", "inputRequests": {"k": {
            "method": "elicitation/create", "params": {"mode": "form", "message": "m", "requestedSchema": {
                "type": "object", "properties": {"sala": field}, "required": ["sala"]}}}}}

    for field in ({"type": "string", "const": "sala-mirante"}, {"type": "string", "enum": ["sala-mirante"]}):
        stub = StubMcpServer(call_result=pause(field))
        outcome = run_with_host("http://stub/mcp", lambda host: host.book(booking(4)), transport=stub)
        assert outcome.choices == ("sala-mirante",)


# --- the retry ---------------------------------------------------------------------------------------------------------


def test_resume_repeats_the_call_with_a_new_id_the_same_key_and_the_untouched_state(server, days):
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        pending = await host.book(booking(day, "sala-garagem", 14, 15, "Marty"))
        return pending, await host.resume(pending, Answer.accept("sala-fusca"))

    (pending, outcome), wire = host_run(server, scenario)
    first, retry = wire.calls("tools/call")[-2:]
    assert retry.request["id"] != first.request["id"]
    assert retry.request["params"]["arguments"] == first.request["params"]["arguments"]
    assert retry.request["params"]["inputResponses"] == {
        pending.key: {"action": "accept", "content": {"sala": "sala-fusca"}}}
    assert retry.request["params"]["requestState"] == first.response["result"]["requestState"] == pending.request_state
    assert "inputRequests" not in retry.request["params"]
    assert isinstance(outcome, Reserved) and outcome.reservation["sala"] == "sala-fusca"
    assert outcome.reservation["responsavel"] == "Marty" and outcome.policy_version == "2026-11-01"


def test_every_jsonrpc_id_on_the_wire_is_unique_across_the_whole_session(server, days):
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        pending = await host.book(booking(day, "sala-garagem", 14, 15, "Marty"))
        await host.resume(pending, Answer.accept("sala-mirante"))
        await host.book(booking(next(days)))

    _, wire = host_run(server, scenario)
    ids = [e.request["id"] for e in wire.calls()]
    assert len(ids) == len(set(ids)) and ids == sorted(ids)


def test_declining_completes_without_a_reservation(server, days):
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        pending = await host.book(booking(day, "sala-garagem", 14, 15, "Marty"))
        return await host.resume(pending, Answer.decline())

    outcome, wire = host_run(server, scenario)
    assert isinstance(outcome, Declined) and outcome.reason == "recusado"
    assert wire.calls("tools/call")[-1].request["params"]["inputResponses"] == {
        next(iter(wire.calls("tools/call")[-1].request["params"]["inputResponses"])): {"action": "decline"}}
    client = McpHttpClient(server.url)
    assert is_free(client, "sala-fusca", day) and is_free(client, "sala-mirante", day)


def test_a_forged_state_is_a_protocol_error_that_never_repeats_the_token(server, days):
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        pending = await host.book(booking(day, "sala-garagem", 14, 15, "Marty"))
        forged = dataclasses.replace(pending, request_state=pending.request_state[:-3] + "AAA")
        with pytest.raises(McpProtocolError) as error:
            await host.resume(forged, Answer.accept("sala-fusca"))
        return pending, error.value

    (pending, error), _ = host_run(server, scenario)
    assert error.code == -32602
    assert pending.request_state not in str(error) and pending.request_state[:30] not in repr(error)
    assert is_free(McpHttpClient(server.url), "sala-fusca", day)


def test_a_stale_question_comes_back_as_a_new_pause_with_a_new_state(server, days):
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        first = await host.book(booking(day, "sala-garagem", 14, 15, "Primeiro"))
        second = await host.book(booking(day, "sala-garagem", 14, 15, "Segundo"))
        await host.resume(second, Answer.accept("sala-mirante"))
        stale = await host.resume(first, Answer.accept("sala-mirante"))
        assert isinstance(stale, InputRequired) and stale.request_state != first.request_state
        assert stale.choices == ("sala-fusca",) and stale.request == first.request
        return await host.resume(stale, Answer.accept("sala-fusca"))

    outcome, wire = host_run(server, scenario)
    assert isinstance(outcome, Reserved) and outcome.reservation["sala"] == "sala-fusca"
    ids = [e.request["id"] for e in wire.calls("tools/call")]
    assert len(ids) == len(set(ids))


# --- trace propagation --------------------------------------------------------------------------------------------------


def test_the_trace_id_reaches_every_request_of_a_paused_and_resumed_booking(server, days):
    day = next(days)

    async def scenario(host):
        await occupy(host, "sala-garagem", day)
        pending = await host.book(booking(day, "sala-garagem", 14, 15, "Marty"), traceparent=TRACEPARENT)
        await host.resume(pending, Answer.accept("sala-fusca"), traceparent=TRACEPARENT)

    _, wire = host_run(server, scenario)
    traced = [e for e in wire.calls() if "traceparent" in e.request["params"]["_meta"]]
    assert [e.request["method"] for e in traced] == ["tools/list", "resources/read", "tools/call", "tools/call"]
    contexts = [parse_traceparent(e.request["params"]["_meta"]["traceparent"]) for e in traced]
    assert {c.trace_id for c in contexts} == {TRACE_ID} and {c.flags for c in contexts} == {"01"}
    assert len({c.span_id for c in contexts}) == len(contexts)


def test_concurrent_bookings_keep_their_own_trace_ids(server, days):
    first_day, second_day = next(days), next(days)
    other_trace = "11111111111111111111111111111111"

    async def scenario(host):
        await asyncio.gather(
            host.book(booking(first_day), traceparent=TRACEPARENT),
            host.book(booking(second_day, "sala-porao"), traceparent=f"00-{other_trace}-aaaaaaaaaaaaaaaa-01"),
        )

    _, wire = host_run(server, scenario)
    by_trace = {}
    for exchange in wire.calls():
        context = parse_traceparent(exchange.request["params"]["_meta"]["traceparent"])
        by_trace.setdefault(context.trace_id, []).append(exchange.request["method"])
    assert sorted(by_trace) == sorted([TRACE_ID, other_trace])
    assert all(sorted(methods) == ["resources/read", "tools/call", "tools/list"] for methods in by_trace.values())


@pytest.mark.parametrize("bad", ["garbage", "", None, f"00-{'0' * 32}-00f067aa0ba902b7-01"])
def test_an_invalid_traceparent_is_dropped_not_forwarded_and_not_fatal(server, days, bad):
    outcome, wire = host_run(server, lambda host: host.book(booking(next(days)), traceparent=bad))
    assert isinstance(outcome, Reserved)
    assert all("traceparent" not in e.request["params"]["_meta"] for e in wire.calls())


# --- availability -------------------------------------------------------------------------------------------------------


def test_an_unreachable_server_is_a_clean_error_without_urls_or_internals(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(McpUnavailable) as error:
        run_with_host(f"http://127.0.0.1:{port}/mcp", lambda host: host.book(booking(4)))
    assert "127.0.0.1" not in str(error.value) and "Traceback" not in str(error.value)


def test_the_same_host_recovers_by_itself_when_the_server_comes_back(tmp_path_factory):
    first = McpServerProcess(tmp_path_factory.mktemp("up")).start()
    recorder = RecordingTransport()

    async def scenario():
        async with McpHost(first.url + "/mcp", http_transport=recorder) as host:
            assert isinstance(await host.book(booking(4)), Reserved)
            first.stop()
            with pytest.raises(McpUnavailable):
                await host.book(booking(5))
            second = McpServerProcess(tmp_path_factory.mktemp("back"), port=first.port).start()
            try:
                return await host.book(booking(6))
            finally:
                second.stop()

    try:
        outcome = asyncio.run(scenario())
    finally:
        first.stop()
    assert isinstance(outcome, Reserved)
    ids = [e.request["id"] for e in recorder.calls()]
    assert len(ids) == len(set(ids)), "JSON-RPC ids must stay unique across the outage"


def test_two_hosts_do_not_share_state(server, days):
    day = next(days)

    async def scenario(host):
        return await host.book(booking(day))

    first, _ = host_run(server, scenario)
    second, _ = host_run(server, lambda host: host.book(booking(day, "sala-porao")))
    assert isinstance(first, Reserved) and isinstance(second, Reserved)


# --- stderr evidence of the MCP server ------------------------------------------------------------------------------------


def log_lines(server) -> list[str]:
    return [line for line in server.stderr_text().splitlines() if line.startswith("mcp-request ")]


def field(line: str, name: str) -> str:
    return re.search(rf"\b{name}=(\S+)", line).group(1)


def test_the_server_log_shows_discovery_before_the_first_call_and_distinct_ids_on_the_retry(fresh_server):
    async def scenario(host):
        pending = await host.book(booking(3, "sala-garagem", 14, 15, "Marty"), traceparent=TRACEPARENT)
        await host.resume(pending, Answer.accept("sala-mirante"), traceparent=TRACEPARENT)

    run_with_host(fresh_server.url + "/mcp", scenario)
    lines = log_lines(fresh_server)
    methods = [field(line, "method") for line in lines]
    assert methods == ["tools/list", "resources/read", "tools/call", "tools/call"]
    calls = [line for line in lines if field(line, "method") == "tools/call"]
    assert {field(line, "name") for line in calls} == {"reservar_sala"}
    assert field(calls[0], "id") != field(calls[1], "id")
    traces = {parse_traceparent(field(line, "traceparent")).trace_id for line in lines}
    assert traces == {TRACE_ID}


# --- architecture guards ------------------------------------------------------------------------------------------------


AGENT_SOURCES = sorted((ROOT / "agente").glob("*.py"))


def test_the_agent_never_imports_the_server_code():
    forbidden = re.compile(r"^\s*(from|import)\s+(domain|repository|service|protocol|telemetry|config|servidor)\b",
                           re.MULTILINE)
    assert AGENT_SOURCES
    for source in AGENT_SOURCES:
        assert not forbidden.search(source.read_text(encoding="utf-8")), source.name


def test_the_agent_carries_no_catalogue_no_rooms_and_no_policy_values():
    banned = ["listar_salas", "consultar_disponibilidade", "sala-aquario", "sala-porao", "sala-garagem",
              "sala-fusca", "sala-mirante", "2026-11-01", "08:00", "20:00"]
    for source in AGENT_SOURCES:
        text = source.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{source.name} hardcodes {token!r}"
