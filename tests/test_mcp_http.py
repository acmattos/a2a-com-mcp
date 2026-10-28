"""Phase 2 integration tests: the real server process, real HTTP, raw JSON-RPC."""

import json

import httpx
import pytest

from conftest import (CAPABILITIES_KEY, ELICITATION_FORM, PROTOCOL, ROOT, VERSION_KEY, McpHttpClient,
                      McpServerProcess)
from protocol import ERR_CONFLICT_PROVISIONAL

DATA = ROOT / "dados"

ERR_UNKNOWN_ROOM = "Sala inexistente: sala-delorean"
ERR_WINDOW = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERR_DURATION = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERR_INTERVAL = "Intervalo invalido: fim deve ser posterior a inicio"


def at(hour: int, minute: int = 0) -> str:
    return f"2026-11-03T{hour:02d}:{minute:02d}:00-03:00"


def booking(room="sala-aquario", start=(9,), end=(10,), owner="Doc") -> dict:
    return {"sala": room, "inicio": at(*start), "fim": at(*end), "responsavel": owner}


def text_of(result: dict) -> str:
    return " ".join(block.get("text", "") for block in result.get("content", []))


# --- transport ----------------------------------------------------------------------------------------


def test_endpoint_answers_a_plain_post_directly_without_redirect(server):
    response = httpx.post(
        server.url + "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
              "params": {"_meta": {VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: ELICITATION_FORM}}},
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": "tools/list"},
        follow_redirects=False,
    )
    assert response.status_code == 200


def test_response_is_json_with_the_request_id(mcp):
    status, body, response = mcp.call("tools/list", request_id="abc-1")
    assert status == 200
    assert response.headers["content-type"].startswith("application/json")
    assert body["jsonrpc"] == "2.0" and body["id"] == "abc-1"
    assert body["result"]["resultType"] == "complete"


def test_server_is_reachable_through_the_localhost_name(server):
    status, body, _ = McpHttpClient(f"http://localhost:{server.port}").call("tools/list")
    assert status == 200 and "tools" in body["result"]


def test_server_declares_tools_and_resources_capabilities(mcp):
    _, body, _ = mcp.call("server/discover", mirrored_name=None)
    capabilities = body["result"]["capabilities"]
    assert "tools" in capabilities and "resources" in capabilities


# --- tools/list and schemas --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tools(mcp):
    _, body, _ = mcp.call("tools/list")
    return {tool["name"]: tool for tool in body["result"]["tools"]}


def test_exactly_the_three_tools_are_registered(tools):
    assert set(tools) == {"listar_salas", "consultar_disponibilidade", "reservar_sala"}


def test_every_input_schema_is_an_object_schema(tools):
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools.values())


def test_listar_salas_takes_no_arguments_and_declares_an_output_schema(tools):
    tool = tools["listar_salas"]
    assert tool["inputSchema"].get("properties", {}) == {} and not tool["inputSchema"].get("required")
    assert tool["outputSchema"]["type"] == "object" and "salas" in tool["outputSchema"]["properties"]


def test_required_arguments_of_the_other_tools(tools):
    assert set(tools["consultar_disponibilidade"]["inputSchema"]["required"]) == {"sala", "inicio", "fim"}
    assert set(tools["reservar_sala"]["inputSchema"]["required"]) == {"sala", "inicio", "fim", "responsavel"}


def test_output_schemas_describe_the_documented_fields(tools):
    assert {"sala", "livre", "conflitos"} <= set(tools["consultar_disponibilidade"]["outputSchema"]["properties"])
    assert {"reserva", "reservado", "sala", "inicio", "fim", "responsavel", "politica", "motivo"} <= set(
        tools["reservar_sala"]["outputSchema"]["properties"]
    )


# --- listar_salas ----------------------------------------------------------------------------------------------


def test_listar_salas_returns_all_rooms_from_the_data_file(mcp):
    result = mcp.tool_result("listar_salas")
    expected = json.loads((DATA / "salas.json").read_text(encoding="utf-8"))
    assert result["isError"] is False and result["resultType"] == "complete"
    assert result["structuredContent"] == {"salas": expected}


def test_listar_salas_structured_and_text_are_the_same_json(mcp):
    result = mcp.tool_result("listar_salas")
    assert json.loads(text_of(result)) == result["structuredContent"]


# --- consultar_disponibilidade ----------------------------------------------------------------------------------


def test_free_interval_is_reported_free(mcp):
    result = mcp.tool_result("consultar_disponibilidade", booking("sala-aquario", (9,), (10,)))
    assert result["structuredContent"] == {"sala": "sala-aquario", "livre": True, "conflitos": []}
    assert json.loads(text_of(result)) == result["structuredContent"]


def test_busy_interval_lists_the_conflicting_reservation(mcp):
    result = mcp.tool_result("consultar_disponibilidade", booking("sala-garagem", (14, 30), (15, 30)))
    assert result["structuredContent"] == {
        "sala": "sala-garagem",
        "livre": False,
        "conflitos": [{"id": "res-0001", "inicio": at(14), "fim": at(15), "responsavel": "Marty"}],
    }


def test_adjacent_interval_is_free(mcp):
    assert mcp.tool_result("consultar_disponibilidade", booking("sala-garagem", (15,), (16,)))[
        "structuredContent"]["livre"] is True


@pytest.mark.parametrize("tool", ["consultar_disponibilidade", "reservar_sala"])
@pytest.mark.parametrize(
    "arguments,message",
    [
        (booking("sala-delorean"), ERR_UNKNOWN_ROOM),
        (booking(start=(7,), end=(8,)), ERR_WINDOW),
        (booking(start=(19,), end=(21,)), ERR_WINDOW),
        (booking(start=(9,), end=(12,)), ERR_DURATION),
        (booking(start=(10,), end=(9,)), ERR_INTERVAL),
        (booking(start=(10,), end=(10,)), ERR_INTERVAL),
    ],
)
def test_business_errors_are_tool_errors_with_the_exact_message(mcp, tool, arguments, message):
    status, body, _ = mcp.call("tools/call", {"name": tool, "arguments": arguments})
    result = body["result"]
    assert status == 200 and "error" not in body
    assert result["isError"] is True and result["resultType"] == "complete"
    assert result["content"] == [{"type": "text", "text": message}]


@pytest.mark.parametrize("start,end", [((8,), (9,)), ((19,), (20,)), ((9,), (11,))])
def test_window_and_duration_limits_are_inclusive_over_the_wire(mcp, start, end):
    result = mcp.tool_result("consultar_disponibilidade", booking("sala-porao", start, end))
    assert result["isError"] is False and result["structuredContent"]["livre"] is True


def test_malformed_instant_is_a_tool_error_not_a_server_failure(mcp):
    arguments = booking()
    arguments["inicio"] = "amanha de manha"
    status, body, _ = mcp.call("tools/call", {"name": "consultar_disponibilidade", "arguments": arguments})
    assert status == 200 and body["result"]["isError"] is True


def test_missing_required_argument_is_rejected_without_a_server_failure(mcp):
    status, body, _ = mcp.call("tools/call", {"name": "reservar_sala", "arguments": {"sala": "sala-aquario"}})
    assert status < 500
    assert body.get("error") or body["result"]["isError"] is True


def test_unknown_tool_is_refused(mcp):
    _, body, _ = mcp.call("tools/call", {"name": "voar_delorean", "arguments": {}})
    assert body.get("error", {}).get("code") == -32602 or body["result"]["isError"] is True


# --- reservar_sala (free path) -----------------------------------------------------------------------------------


def test_reserve_creates_the_booking_and_reports_the_policy_from_the_file(fresh_mcp):
    result = fresh_mcp.tool_result("reservar_sala", booking("sala-aquario", (9,), (10,), "Doc"))
    policy_version = (DATA / "politica-de-uso.md").read_text(encoding="utf-8").splitlines()[0].split(":")[1].strip()
    assert result["isError"] is False and result["resultType"] == "complete"
    assert result["structuredContent"] == {
        "reserva": "res-0003", "reservado": True, "sala": "sala-aquario", "inicio": at(9), "fim": at(10),
        "responsavel": "Doc", "politica": policy_version, "motivo": None,
    }
    assert json.loads(text_of(result)) == result["structuredContent"]


def test_reservations_persist_for_later_queries_in_the_same_process(fresh_mcp):
    first = fresh_mcp.tool_result("reservar_sala", booking("sala-aquario", (9,), (10,)))["structuredContent"]
    availability = fresh_mcp.tool_result("consultar_disponibilidade", booking("sala-aquario", (9, 30), (10, 30)))
    assert availability["structuredContent"]["livre"] is False
    assert availability["structuredContent"]["conflitos"][0]["id"] == first["reserva"]
    second = fresh_mcp.tool_result("reservar_sala", booking("sala-aquario", (10,), (11,)))["structuredContent"]
    assert second["reserva"] == "res-0004"


def test_booking_the_same_slot_twice_is_not_possible(fresh_mcp):
    fresh_mcp.tool_result("reservar_sala", booking("sala-aquario", (9,), (10,)))
    again = fresh_mcp.tool_result("reservar_sala", booking("sala-aquario", (9,), (10,), "Outro"))
    assert again["isError"] is True
    conflicts = fresh_mcp.tool_result("consultar_disponibilidade", booking("sala-aquario", (9,), (10,)))
    assert [c["responsavel"] for c in conflicts["structuredContent"]["conflitos"]] == ["Doc"]


def test_conflict_with_an_initial_reservation_is_refused_without_creating_anything(fresh_mcp):
    """Provisional phase-2 behaviour: plain tool error. The multi-round flow replaces it in phase 3."""
    result = fresh_mcp.tool_result("reservar_sala", booking("sala-garagem", (14,), (15,), "Biff"))
    assert result["isError"] is True and text_of(result) == ERR_CONFLICT_PROVISIONAL
    assert "inputRequests" not in result and "requestState" not in result
    conflicts = fresh_mcp.tool_result("consultar_disponibilidade", booking("sala-garagem", (14,), (15,)))
    assert [c["id"] for c in conflicts["structuredContent"]["conflitos"]] == ["res-0001"]
    assert fresh_mcp.tool_result("reservar_sala", booking("sala-porao", (9,), (10,)))["structuredContent"][
        "reserva"] == "res-0003"


def test_invalid_booking_creates_nothing(fresh_mcp):
    fresh_mcp.tool_result("reservar_sala", booking("sala-delorean"))
    fresh_mcp.tool_result("reservar_sala", booking(start=(7,), end=(8,)))
    assert fresh_mcp.tool_result("reservar_sala", booking())["structuredContent"]["reserva"] == "res-0003"


def test_state_is_reset_by_a_new_process(fresh_mcp, tmp_path_factory):
    fresh_mcp.tool_result("reservar_sala", booking("sala-aquario", (9,), (10,)))
    other = McpServerProcess(tmp_path_factory.mktemp("mcp-other")).start()
    try:
        availability = McpHttpClient(other.url).tool_result("consultar_disponibilidade", booking("sala-aquario"))
        assert availability["structuredContent"]["livre"] is True
    finally:
        other.stop()


# --- resource ----------------------------------------------------------------------------------------------------


def test_policy_resource_is_listed(mcp):
    _, body, _ = mcp.call("resources/list", mirrored_name=None)
    listed = {r["uri"]: r for r in body["result"]["resources"]}
    assert listed["politica://uso"]["mimeType"] == "text/markdown"


def test_policy_resource_returns_the_whole_markdown_file(mcp):
    status, body, _ = mcp.call("resources/read", {"uri": "politica://uso"})
    [content] = body["result"]["contents"]
    expected = (DATA / "politica-de-uso.md").read_text(encoding="utf-8")
    assert status == 200 and body["result"]["resultType"] == "complete"
    assert content["uri"] == "politica://uso" and content["mimeType"] == "text/markdown"
    assert content["text"] == expected and content["text"].startswith("versao: 2026-11-01")


@pytest.mark.parametrize("uri", ["politica://inexistente", "politica://uso/", "politica://", "file:///etc/passwd"])
def test_unknown_resource_is_invalid_params_never_empty_contents(mcp, uri):
    _, body, _ = mcp.call("resources/read", {"uri": uri})
    assert body["error"]["code"] == -32602
    assert "result" not in body


# --- required _meta -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method,params", [
    ("tools/list", {}),
    ("tools/call", {"name": "listar_salas", "arguments": {}}),
    ("resources/read", {"uri": "politica://uso"}),
])
@pytest.mark.parametrize("missing", [VERSION_KEY, CAPABILITIES_KEY])
def test_missing_required_meta_key_is_invalid_params_with_http_400(mcp, method, params, missing):
    meta = {VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: ELICITATION_FORM}
    del meta[missing]
    status, body, _ = mcp.call(method, params, meta=meta)
    assert status == 400 and body["error"]["code"] == -32602


def test_missing_meta_object_is_invalid_params_with_http_400(mcp):
    status, body, _ = mcp.call("tools/list", meta=None)
    assert status == 400 and body["error"]["code"] == -32602


def test_nothing_is_inferred_from_previous_requests(mcp):
    assert mcp.call("tools/list")[0] == 200
    assert mcp.call("tools/list", meta={})[0] == 400
    assert mcp.call("tools/list")[0] == 200


def test_empty_capabilities_are_valid_for_non_eliciting_calls(mcp):
    status, body, _ = mcp.call("tools/call", {"name": "listar_salas", "arguments": {}},
                               meta={VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: {}})
    assert status == 200 and body["result"]["isError"] is False


# --- mirrored headers --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [
    {"Mcp-Method": "tools/list"},
    {"MCP-Protocol-Version": "1999-01-01"},
])
def test_inconsistent_method_or_unknown_version_header_is_header_mismatch(mcp, headers):
    status, body, _ = mcp.call("tools/call", {"name": "listar_salas", "arguments": {}}, headers=headers)
    assert body["error"]["code"] == -32020 and status == 400


@pytest.mark.xfail(strict=True, reason=(
    "SDK limitation (mcp 2.3.0): a MCP-Protocol-Version header that names a legacy revision, or is absent, "
    "routes the POST to the legacy transport, which never runs the envelope ladder in mcp/shared/inbound.py. "
    "A body carrying the 2026-07-28 envelope is then served instead of rejected with -32020. See docs/decisoes.md."
))
@pytest.mark.parametrize("version_header", ["2025-11-25", None])
def test_legacy_or_absent_version_header_against_a_modern_envelope_is_header_mismatch(mcp, version_header):
    status, body, _ = mcp.call("tools/call", {"name": "listar_salas", "arguments": {}},
                               headers={"MCP-Protocol-Version": version_header})
    assert body.get("error", {}).get("code") == -32020 and status == 400


def test_inconsistent_name_header_is_header_mismatch(mcp):
    status, body, _ = mcp.call("tools/call", {"name": "listar_salas", "arguments": {}},
                               headers={"Mcp-Name": "reservar_sala"})
    assert body["error"]["code"] == -32020 and status == 400


def test_inconsistent_name_header_on_resources_read_is_header_mismatch(mcp):
    status, body, _ = mcp.call("resources/read", {"uri": "politica://uso"}, headers={"Mcp-Name": "politica://outra"})
    assert body["error"]["code"] == -32020 and status == 400


def test_missing_name_header_on_tools_call_is_header_mismatch(mcp):
    status, body, _ = mcp.call("tools/call", {"name": "listar_salas", "arguments": {}}, mirrored_name=None)
    assert body["error"]["code"] == -32020 and status == 400


def test_consistent_headers_are_accepted(mcp):
    assert mcp.call("tools/call", {"name": "listar_salas", "arguments": {}})[0] == 200


# --- stderr logging ---------------------------------------------------------------------------------------------------------


TRACE = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def request_lines(server) -> list[str]:
    return [line for line in server.stderr_text().splitlines() if line.startswith("mcp-request ")]


def test_every_request_is_logged_with_method_id_and_traceparent(fresh_server, fresh_mcp):
    meta = {VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: ELICITATION_FORM, "traceparent": TRACE}
    fresh_mcp.call("tools/list", meta=meta, request_id="req-list")
    fresh_mcp.call("tools/call", {"name": "listar_salas", "arguments": {}}, meta=meta, request_id=42)
    fresh_mcp.call("resources/read", {"uri": "politica://uso"}, meta=meta, request_id="req-res")
    lines = request_lines(fresh_server)
    assert any("method=tools/list" in l and "id=req-list" in l and f"traceparent={TRACE}" in l for l in lines)
    assert any("method=tools/call" in l and "id=42" in l and "name=listar_salas" in l and TRACE in l for l in lines)
    assert any("method=resources/read" in l and "id=req-res" in l and TRACE in l for l in lines)


def test_rejected_requests_are_logged_too(fresh_server, fresh_mcp):
    fresh_mcp.call("tools/list", meta={}, request_id="no-meta")
    assert any("method=tools/list" in l and "id=no-meta" in l for l in request_lines(fresh_server))


def test_request_without_traceparent_is_logged_with_a_dash(fresh_server, fresh_mcp):
    fresh_mcp.call("tools/list", request_id="plain")
    [line] = [l for l in request_lines(fresh_server) if "id=plain" in l]
    assert line.endswith("traceparent=-")


def test_arguments_and_owner_names_never_reach_the_log(fresh_server, fresh_mcp):
    fresh_mcp.tool_result("reservar_sala", booking(owner="NomeSensivelXYZ"))
    log = fresh_server.stderr_text()
    assert "NomeSensivelXYZ" not in log and "requestState" not in log


def test_server_survives_garbage_bodies(fresh_server):
    response = httpx.post(fresh_server.url + "/mcp", content=b"{not json", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    assert response.status_code in (400, 422)
    assert httpx.post(fresh_server.url + "/mcp", content=b"[]", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}).status_code < 500
    assert any("method=<unparseable>" in l for l in request_lines(fresh_server))
