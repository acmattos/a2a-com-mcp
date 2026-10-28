"""Phase 3: multi-round-trip flow, sealed requestState and its attack surface. Real server, real HTTP."""

import itertools
import json

import pytest

from conftest import (CAPABILITIES_KEY, ELICITATION_FORM, PROTOCOL, VERSION_KEY, McpHttpClient,
                      McpServerProcess)
from mrtr_helpers import (ERR_NO_ALTERNATIVES, POLICY, QUESTION, Pending, accept, args, ask, at, conflict,
                          flipped, holders, is_free, occupy, reserve, respond, text_of)


@pytest.fixture(scope="module")
def days():
    """A different day per scenario, so scenarios sharing one server never interact."""
    return itertools.count(4)


# --- the question ----------------------------------------------------------------------------------------------


def test_conflict_answers_input_required_with_one_form_elicitation(mcp, days):
    pending = conflict(mcp, next(days))
    result = pending.result
    assert result["resultType"] == "input_required"
    assert "isError" not in result and "content" not in result
    assert len(result["inputRequests"]) == 1
    request = pending.request
    assert request["method"] == "elicitation/create"
    assert request["params"]["mode"] == "form"
    assert request["params"]["message"] == QUESTION
    assert isinstance(pending.state, str) and pending.state


def test_schema_is_flat_with_a_single_required_string_property_limited_to_the_alternatives(mcp, days):
    schema = conflict(mcp, next(days)).request["params"]["requestedSchema"]
    assert schema["type"] == "object"
    assert list(schema["properties"]) == ["sala"] and schema["required"] == ["sala"]
    assert schema["properties"]["sala"]["type"] == "string"
    assert schema["properties"]["sala"]["enum"] == ["sala-fusca", "sala-mirante"]


def test_alternatives_follow_capacity_then_id_and_are_capped_at_three(mcp, days):
    assert conflict(mcp, next(days), room="sala-aquario").choices == ["sala-porao", "sala-fusca", "sala-garagem"]


def test_equal_capacity_rooms_are_offered_and_smaller_ones_never_are(mcp, days):
    assert conflict(mcp, next(days), room="sala-fusca").choices == ["sala-garagem", "sala-mirante"]


def test_a_single_alternative_is_offered_as_enum_or_const(mcp, days):
    day = next(days)
    occupy(mcp, "sala-fusca", day)
    assert conflict(mcp, day).choices == ["sala-mirante"]


def test_the_wire_key_is_assigned_by_the_server_and_is_stable(mcp, days):
    first, second = conflict(mcp, next(days)), conflict(mcp, next(days))
    assert first.key == second.key and isinstance(first.key, str) and first.key


def test_a_conflict_with_the_seed_reservation_is_asked_too(fresh_mcp):
    assert ask(fresh_mcp).choices == ["sala-fusca", "sala-mirante"]


def test_asking_creates_no_reservation(mcp, days):
    day = next(days)
    pending = conflict(mcp, day)
    assert holders(mcp, pending.arguments["sala"], day) == ["Ocupante"]
    assert is_free(mcp, "sala-fusca", day) and is_free(mcp, "sala-mirante", day)


# --- no alternatives ---------------------------------------------------------------------------------------------


def test_no_alternative_is_a_tool_error_without_any_elicitation(mcp, days):
    day = next(days)
    for room in ("sala-fusca", "sala-mirante"):
        occupy(mcp, room, day)
    occupy(mcp, "sala-garagem", day)
    result = reserve(mcp, day=day)
    assert result["isError"] is True and result["resultType"] == "complete"
    assert result["content"] == [{"type": "text", "text": ERR_NO_ALTERNATIVES}]
    assert "inputRequests" not in result and "requestState" not in result


def test_the_largest_room_has_no_alternatives(mcp, days):
    day = next(days)
    occupy(mcp, "sala-mirante", day)
    result = reserve(mcp, room="sala-mirante", day=day)
    assert result["isError"] is True and text_of(result) == ERR_NO_ALTERNATIVES


def test_no_alternative_does_not_need_the_elicitation_capability(mcp, days):
    day = next(days)
    occupy(mcp, "sala-mirante", day)
    meta = {VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: {}}
    result = mcp.tool_result("reservar_sala", args(room="sala-mirante", day=day), meta=meta)
    assert result["isError"] is True and text_of(result) == ERR_NO_ALTERNATIVES


# --- answering ---------------------------------------------------------------------------------------------------


def test_accepting_an_alternative_completes_the_booking_in_that_room(mcp, days):
    day = next(days)
    pending = conflict(mcp, day)
    body = respond(mcp, pending, accept("sala-fusca"))
    result = body["result"]
    assert result["resultType"] == "complete" and result["isError"] is False
    structured = result["structuredContent"]
    assert structured["reservado"] is True and structured["sala"] == "sala-fusca"
    assert structured["inicio"] == at(day, 14) and structured["fim"] == at(day, 15)
    assert structured["responsavel"] == "Marty" and structured["politica"] == POLICY
    assert structured["reserva"].startswith("res-") and structured["motivo"] is None
    assert json.loads(text_of(result)) == structured


def test_the_accepted_booking_is_visible_and_the_original_room_is_untouched(mcp, days):
    day = next(days)
    pending = conflict(mcp, day)
    respond(mcp, pending, accept("sala-mirante"))
    assert holders(mcp, "sala-mirante", day) == ["Marty"]
    assert holders(mcp, "sala-garagem", day) == ["Ocupante"]


def test_the_retry_uses_a_new_jsonrpc_id(mcp, days):
    pending = conflict(mcp, next(days))
    body = respond(mcp, pending, accept("sala-fusca"))
    assert body["id"] != pending.request_id and body["result"]["resultType"] == "complete"


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_refusing_completes_without_error_and_without_a_reservation(mcp, days, action):
    day = next(days)
    pending = conflict(mcp, day)
    result = respond(mcp, pending, {"action": action})["result"]
    assert result["resultType"] == "complete" and result.get("isError", False) is False
    assert result["structuredContent"]["reservado"] is False
    assert result["structuredContent"]["motivo"] == "recusado"
    assert result["structuredContent"]["reserva"] is None and result["structuredContent"]["sala"] is None
    assert json.loads(text_of(result)) == result["structuredContent"]
    assert is_free(mcp, "sala-fusca", day) and is_free(mcp, "sala-mirante", day)
    assert holders(mcp, "sala-garagem", day) == ["Ocupante"]


def test_a_refusal_does_not_burn_a_reservation_id(fresh_mcp):
    pending = ask(fresh_mcp)
    respond(fresh_mcp, pending, {"action": "decline"})
    assert reserve(fresh_mcp, room="sala-porao", day=3, start=9, end=10)["structuredContent"]["reserva"] == "res-0003"


def test_replaying_an_accepted_answer_cannot_book_twice(mcp, days):
    day = next(days)
    pending = conflict(mcp, day)
    respond(mcp, pending, accept("sala-fusca"))
    again = respond(mcp, pending, accept("sala-fusca"))
    result = again.get("result", {})
    assert result.get("resultType") != "complete" or result.get("isError") is True
    assert holders(mcp, "sala-fusca", day) == ["Marty"]


# --- tampering ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("make", [
    lambda t: flipped(t, len(t) // 2),
    lambda t: flipped(t, len(t) - 1),
    lambda t: flipped(t, 3),
    lambda t: t[:-1],
    lambda t: t + "A",
    lambda t: t[: len(t) // 2],
    lambda t: "v1.garbage",
    lambda t: "not-a-token",
    lambda t: "v2." + t[3:],
], ids=["middle", "last", "first", "truncated", "appended", "half", "garbage", "plain", "version"])
def test_a_tampered_request_state_is_rejected_with_invalid_params_and_books_nothing(mcp, days, make):
    day = next(days)
    pending = conflict(mcp, day)
    status, body, _ = mcp.call("tools/call", {
        "name": "reservar_sala", "arguments": pending.arguments,
        "inputResponses": {pending.key: accept("sala-fusca")}, "requestState": make(pending.state)})
    assert status == 400 and body["error"]["code"] == -32602
    assert "result" not in body
    assert is_free(mcp, "sala-fusca", day) and is_free(mcp, "sala-mirante", day)


def test_the_original_token_still_works_after_a_rejected_forgery(mcp, days):
    pending = conflict(mcp, next(days))
    respond(mcp, pending, accept("sala-fusca"), state=flipped(pending.state, 10))
    assert respond(mcp, pending, accept("sala-fusca"))["result"]["structuredContent"]["sala"] == "sala-fusca"


@pytest.mark.parametrize("field,value", [
    ("sala", "sala-aquario"),
    ("inicio", at(4, 9)),
    ("fim", at(4, 16)),
    ("responsavel", "Biff"),
])
def test_changing_any_original_argument_on_retry_is_rejected(mcp, days, field, value):
    day = next(days)
    pending = conflict(mcp, day)
    forged = {**pending.arguments, field: value}
    status, body, _ = mcp.call("tools/call", {
        "name": "reservar_sala", "arguments": forged,
        "inputResponses": {pending.key: accept("sala-fusca")}, "requestState": pending.state})
    assert status == 400 and body["error"]["code"] == -32602
    assert is_free(mcp, "sala-fusca", day) and is_free(mcp, "sala-aquario", day, 14, 15)


def test_a_token_is_bound_to_its_own_request(mcp, days):
    first, second = conflict(mcp, next(days), owner="Primeiro"), conflict(mcp, next(days), owner="Segundo")
    body = respond(mcp, first, accept("sala-fusca"), arguments=second.arguments)
    assert body["error"]["code"] == -32602


def test_a_token_is_bound_to_its_tool(mcp, days):
    day = next(days)
    pending = conflict(mcp, day)
    body = respond(mcp, pending, accept("sala-fusca"), tool="consultar_disponibilidade")
    assert body.get("error", {}).get("code") == -32602 or body["result"]["isError"] is True
    assert is_free(mcp, "sala-fusca", day)


# --- invalid answers -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("room", ["sala-aquario", "sala-porao", "sala-delorean", "", "SALA-FUSCA"])
def test_a_choice_outside_the_offered_rooms_books_nothing(mcp, days, room):
    day = next(days)
    pending = conflict(mcp, day)
    body = respond(mcp, pending, accept(room))
    assert "error" in body or body["result"]["isError"] is True
    for candidate in ("sala-aquario", "sala-porao", "sala-fusca", "sala-mirante"):
        assert is_free(mcp, candidate, day)


def test_the_original_room_cannot_be_chosen_as_its_own_alternative(mcp, days):
    day = next(days)
    pending = conflict(mcp, day)
    body = respond(mcp, pending, accept("sala-garagem"))
    assert "error" in body or body["result"]["isError"] is True
    assert holders(mcp, "sala-garagem", day) == ["Ocupante"]


def test_an_answer_under_the_wrong_key_is_not_accepted(mcp, days):
    day = next(days)
    pending = conflict(mcp, day)
    body = respond(mcp, pending, accept("sala-fusca"), key="outra-chave")
    assert body.get("result", {}).get("resultType") != "complete"
    assert is_free(mcp, "sala-fusca", day)


@pytest.mark.parametrize("response", [
    "accept",
    5,
    None,
    [],
    {},
    {"action": "maybe"},
    {"action": "accept"},
    {"action": "accept", "content": None},
    {"action": "accept", "content": {}},
    {"action": "accept", "content": {"sala": 123}},
    {"action": "accept", "content": {"sala": ["sala-fusca"]}},
    {"action": "accept", "content": {"sala": None}},
    {"action": "accept", "content": "sala-fusca"},
], ids=lambda r: json.dumps(r)[:30])
def test_malformed_answers_are_handled_without_a_server_failure_and_book_nothing(mcp, days, response):
    day = next(days)
    pending = conflict(mcp, day)
    status, body, _ = mcp.call("tools/call", {
        "name": "reservar_sala", "arguments": pending.arguments,
        "inputResponses": {pending.key: response}, "requestState": pending.state})
    assert status < 500 and ("error" in body or "result" in body)
    result = body.get("result", {})
    assert not (result.get("resultType") == "complete" and result.get("isError") is not True)
    assert is_free(mcp, "sala-fusca", day) and is_free(mcp, "sala-mirante", day)


@pytest.mark.parametrize("container", ["text", 7, [1], None])
def test_malformed_input_responses_container_is_handled_without_a_server_failure(mcp, days, container):
    day = next(days)
    pending = conflict(mcp, day)
    status, body, _ = mcp.call("tools/call", {
        "name": "reservar_sala", "arguments": pending.arguments,
        "inputResponses": container, "requestState": pending.state})
    assert status < 500
    assert is_free(mcp, "sala-fusca", day) and is_free(mcp, "sala-mirante", day)


def test_a_missing_answer_with_a_valid_state_asks_again(mcp, days):
    day = next(days)
    pending = conflict(mcp, day)
    status, body, _ = mcp.call("tools/call", {
        "name": "reservar_sala", "arguments": pending.arguments, "requestState": pending.state})
    assert status < 500 and body["result"]["resultType"] != "complete" or body["result"]["isError"] is True
    assert is_free(mcp, "sala-fusca", day)


# --- capability ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("capabilities", [{}, {"elicitation": {"url": {}}}, {"sampling": {}}], ids=["empty", "url-only", "other"])
def test_a_client_without_form_elicitation_gets_32021_with_the_required_capability(mcp, days, capabilities):
    day = next(days)
    occupy(mcp, "sala-garagem", day)
    status, body, _ = mcp.call("tools/call", {"name": "reservar_sala", "arguments": args(day=day)},
                               meta={VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: capabilities})
    assert status == 400 and body["error"]["code"] == -32021
    assert body["error"]["data"]["requiredCapabilities"] == ELICITATION_FORM
    assert "result" not in body
    assert is_free(mcp, "sala-fusca", day) and is_free(mcp, "sala-mirante", day)


def test_a_free_slot_never_needs_the_capability(mcp, days):
    day = next(days)
    meta = {VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: {}}
    result = mcp.tool_result("reservar_sala", args(room="sala-porao", day=day, start=9, end=10), meta=meta)
    assert result["isError"] is False and result["structuredContent"]["reservado"] is True


# --- independent pending requests --------------------------------------------------------------------------------


def test_two_pending_requests_resume_independently_in_reverse_order(mcp, days):
    first_day, second_day = next(days), next(days)
    first = conflict(mcp, first_day, owner="Lorraine")
    second = conflict(mcp, second_day, owner="George")
    assert first.state != second.state
    done_second = respond(mcp, second, accept("sala-mirante"))["result"]["structuredContent"]
    done_first = respond(mcp, first, accept("sala-fusca"))["result"]["structuredContent"]
    assert (done_second["responsavel"], done_second["sala"], done_second["inicio"]) == ("George", "sala-mirante", at(second_day, 14))
    assert (done_first["responsavel"], done_first["sala"], done_first["inicio"]) == ("Lorraine", "sala-fusca", at(first_day, 14))
    assert done_first["reserva"] != done_second["reserva"]


def test_a_pending_question_that_went_stale_is_asked_again_instead_of_double_booking(mcp, days):
    day = next(days)
    occupy(mcp, "sala-garagem", day)
    first = ask(mcp, day=day, owner="Primeiro")
    second = ask(mcp, day=day, owner="Segundo")
    assert respond(mcp, second, accept("sala-mirante"))["result"]["structuredContent"]["sala"] == "sala-mirante"
    stale = respond(mcp, first, accept("sala-mirante"))["result"]
    assert stale["resultType"] == "input_required"
    renewed = Pending(first.arguments, stale)
    assert renewed.choices == ["sala-fusca"] and renewed.state != first.state
    done = respond(mcp, renewed, accept("sala-fusca"))["result"]["structuredContent"]
    assert (done["sala"], done["responsavel"]) == ("sala-fusca", "Primeiro")
    assert holders(mcp, "sala-mirante", day) == ["Segundo"]


# --- restart and secrets -----------------------------------------------------------------------------------------------


def test_the_token_survives_a_real_process_restart_with_the_same_secret(tmp_path_factory):
    first = McpServerProcess(tmp_path_factory.mktemp("before")).start()
    try:
        pending = ask(McpHttpClient(first.url))
    finally:
        first.stop()
    second = McpServerProcess(tmp_path_factory.mktemp("after"), secret=first.secret).start()
    try:
        assert second.port != 0 and second.secret == first.secret
        result = respond(McpHttpClient(second.url), pending, accept("sala-mirante"))["result"]
        assert result["resultType"] == "complete" and result["isError"] is False
        assert result["structuredContent"]["sala"] == "sala-mirante"
        assert result["structuredContent"]["reserva"] == "res-0003"
    finally:
        second.stop()


def test_a_restart_with_a_different_secret_rejects_the_old_token(tmp_path_factory):
    first = McpServerProcess(tmp_path_factory.mktemp("before")).start()
    try:
        pending = ask(McpHttpClient(first.url))
    finally:
        first.stop()
    second = McpServerProcess(tmp_path_factory.mktemp("after")).start()
    try:
        assert second.secret != first.secret
        client = McpHttpClient(second.url)
        status, body, _ = client.call("tools/call", {
            "name": "reservar_sala", "arguments": pending.arguments,
            "inputResponses": {pending.key: accept("sala-mirante")}, "requestState": pending.state})
        assert status == 400 and body["error"]["code"] == -32602
        assert is_free(client, "sala-mirante", 3)
    finally:
        second.stop()


def test_a_token_from_another_deployment_is_rejected_even_if_it_is_well_formed(tmp_path_factory):
    one = McpServerProcess(tmp_path_factory.mktemp("one")).start()
    two = McpServerProcess(tmp_path_factory.mktemp("two")).start()
    try:
        pending = ask(McpHttpClient(one.url))
        status, body, _ = McpHttpClient(two.url).call("tools/call", {
            "name": "reservar_sala", "arguments": pending.arguments,
            "inputResponses": {pending.key: accept("sala-fusca")}, "requestState": pending.state})
        assert status == 400 and body["error"]["code"] == -32602
    finally:
        one.stop()
        two.stop()


# --- nothing sensitive leaves through the log ---------------------------------------------------------------------------


def test_neither_the_secret_nor_any_request_state_reaches_the_log(fresh_server, fresh_mcp):
    pending = ask(fresh_mcp)
    respond(fresh_mcp, pending, accept("sala-fusca"))
    respond(fresh_mcp, pending, accept("sala-fusca"), state=flipped(pending.state, 10))
    log = fresh_server.stderr_text()
    assert fresh_server.secret not in log
    assert pending.state not in log and pending.state[:40] not in log
