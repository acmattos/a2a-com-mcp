"""A2A over real HTTP: the agent and the MCP server are separate processes, the client is plain httpx."""

import json
import re
import secrets
import threading
from pathlib import Path

import httpx
import pytest

from a2a_helpers import TRACE_ID, TRACEPARENT, A2aClient, booking_text, new_day, state_of, task_of, text_of

ROOT = Path(__file__).resolve().parent.parent
POLICY = "2026-11-01"
ERROR_NO_ROOM = "Sala inexistente: sala-delorean"
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED"}


@pytest.fixture
def a2a(stack):
    return A2aClient(stack.rpc_url)


def card_of(stack) -> dict:
    return httpx.get(stack.card_url, timeout=15).json()


# --- Agent Card -----------------------------------------------------------------------------------------------------


def test_the_agent_card_is_served_at_the_well_known_path(stack):
    response = httpx.get(stack.card_url, timeout=15)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["name"] == "Central de Salas"


def test_the_card_matches_the_contract_example_except_for_the_agent_address(stack):
    expected = json.loads((ROOT / "exemplos/wire/07-a2a-agent-card.json").read_text(encoding="utf-8"))
    expected = expected["response"]["body"]
    actual = card_of(stack)
    for card in (expected, actual):
        card["supportedInterfaces"][0].pop("url")
    assert actual == expected


def test_the_card_declares_the_jsonrpc_v1_interface_at_the_a2a_path(stack):
    [interface] = card_of(stack)["supportedInterfaces"]
    assert interface["protocolBinding"] == "JSONRPC" and interface["protocolVersion"] == "1.0"
    assert interface["url"] == stack.agent.url.replace("127.0.0.1", "localhost") + "/a2a"


def test_the_card_has_no_v03_fields(stack):
    card = card_of(stack)
    legacy = {"url", "preferredTransport", "additionalInterfaces", "interfaces", "protocolVersion"}
    assert not legacy & card.keys()
    assert not {"preferredTransport"} & card["supportedInterfaces"][0].keys()


def test_the_card_says_no_streaming_no_push_no_extended_card(stack):
    assert card_of(stack)["capabilities"] == {"streaming": False, "pushNotifications": False,
                                                "extendedAgentCard": False}


def test_the_card_advertises_the_booking_skill_with_the_grammar_the_agent_parses(stack):
    [skill] = card_of(stack)["skills"]
    assert skill["id"] == "reservar-sala"
    from command import BookingCommand, parse_command

    assert all(isinstance(parse_command(example), BookingCommand) for example in skill["examples"])


# --- SendMessage: a free room -----------------------------------------------------------------------------------------


def test_a_free_room_completes_the_task_with_a_reservation_artifact(a2a):
    day = new_day()
    response = a2a.send(booking_text("sala-porao", day, 9, 10, "Doc"), traceparent=TRACEPARENT)
    task = task_of(response)
    assert response["jsonrpc"] == "2.0" and "error" not in response
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    [artifact] = task["artifacts"]
    assert artifact["name"] == "reserva"
    reservation = json.loads(text_of(artifact))
    assert reservation["reservado"] is True and reservation["sala"] == "sala-porao"
    assert reservation["inicio"].startswith(day.isoformat()) and reservation["responsavel"] == "Doc"
    assert reservation["politica"] == POLICY and re.fullmatch(r"res-\d+", reservation["reserva"])


def test_every_new_request_gets_its_own_task_and_context_ids(a2a):
    day = new_day()
    first = task_of(a2a.send(booking_text("sala-porao", day, 9, 10)))
    second = task_of(a2a.send(booking_text("sala-aquario", day, 9, 10)))
    for task in (first, second):
        assert re.fullmatch(r"task-[0-9a-f]{12}", task["id"]) and re.fullmatch(r"ctx-[0-9a-f]{12}", task["contextId"])
    assert first["id"] != second["id"] and first["contextId"] != second["contextId"]


def test_the_message_ids_of_the_caller_are_preserved_in_the_history(a2a):
    message_id = f"msg-{secrets.token_hex(6)}"
    task = task_of(a2a.send(booking_text("sala-porao", new_day(), 11, 12), message_id=message_id))
    assert task["history"][0]["messageId"] == message_id and task["history"][0]["role"] == "ROLE_USER"


def test_the_history_holds_the_request_then_the_agents_answer_and_status_repeats_the_answer(a2a):
    text = booking_text("sala-porao", new_day(), 12, 13)
    task = task_of(a2a.send(text))
    request, answer = task["history"]
    assert text_of(request) == text
    assert answer["role"] == "ROLE_AGENT" and re.fullmatch(r"Reserva res-\d+ confirmada na sala-porao\.", text_of(answer))
    assert answer == task["status"]["message"]
    assert {m["taskId"] for m in task["history"][1:]} == {task["id"]}


def test_the_task_walks_submitted_working_then_the_terminal_state(stack):
    """The first request pins the order through the only observable surface of a synchronous server: the result."""
    a2a = A2aClient(stack.rpc_url)
    response = a2a.rpc("SendMessage", {"message": {
        "messageId": "msg-order", "role": "ROLE_USER", "parts": [{"text": booking_text("sala-porao", new_day(), 13, 14)}]},
        "configuration": {"returnImmediately": True}})
    first = response.json()["result"]["task"]["status"]["state"]
    assert first in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
    task_id = response.json()["result"]["task"]["id"]
    seen = {first}
    for _ in range(200):
        seen.add(a2a.get(task_id)["result"]["status"]["state"])
        if seen & TERMINAL:
            break
    assert seen & TERMINAL == {"TASK_STATE_COMPLETED"}
    assert seen <= {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED"}


# --- GetTask ---------------------------------------------------------------------------------------------------------


def test_get_task_returns_the_current_state_history_and_artifacts(a2a):
    sent = task_of(a2a.send(booking_text("sala-porao", new_day(), 14, 15)))
    got = a2a.get(sent["id"])["result"]
    assert got["id"] == sent["id"] and got["contextId"] == sent["contextId"]
    assert got["status"]["state"] == "TASK_STATE_COMPLETED"
    assert got["history"] == sent["history"] and got["artifacts"] == sent["artifacts"]


def test_get_task_can_trim_the_history(a2a):
    sent = task_of(a2a.send(booking_text("sala-porao", new_day(), 15, 16)))
    assert len(a2a.get(sent["id"], historyLength=1)["result"]["history"]) == 1
    assert "history" not in a2a.get(sent["id"], historyLength=0)["result"]


def test_an_unknown_task_is_an_error_not_an_invented_task(a2a):
    for response in (a2a.get("task-000000000000"), a2a.send("escolha=sala-fusca", task_id="task-000000000000")):
        assert "result" not in response
        assert response["error"]["code"] == -32001 and "not found" in response["error"]["message"]


# --- tool errors -----------------------------------------------------------------------------------------------------


def test_a_nonexistent_room_fails_the_task_with_the_tools_literal_message(a2a):
    task = task_of(a2a.send(booking_text("sala-delorean", new_day(), 9, 10)))
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert text_of(task["status"]["message"]) == ERROR_NO_ROOM
    assert [text_of(m) for m in task["history"] if m["role"] == "ROLE_AGENT"] == [ERROR_NO_ROOM]
    assert not task.get("artifacts")


@pytest.mark.parametrize("start, end, message", [
    (7, 8, "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"),
    (9, 12, "Duracao acima do limite: a politica permite no maximo 2 horas"),
    (10, 9, "Intervalo invalido: fim deve ser posterior a inicio"),
])
def test_every_domain_error_of_the_tool_is_carried_unchanged(a2a, start, end, message):
    task = task_of(a2a.send(booking_text("sala-porao", new_day(), start, end)))
    assert task["status"]["state"] == "TASK_STATE_FAILED" and text_of(task["status"]["message"]) == message
    assert message in [text_of(m) for m in task["history"]]


def test_a_failed_task_is_persisted_as_failed(a2a):
    sent = task_of(a2a.send(booking_text("sala-delorean", new_day(), 9, 10)))
    got = a2a.get(sent["id"])["result"]
    assert got["status"]["state"] == "TASK_STATE_FAILED" and got["history"] == sent["history"]


# --- invalid command -------------------------------------------------------------------------------------------------


def test_text_that_is_not_a_booking_command_is_rejected_with_an_explicit_stable_message(a2a):
    first = task_of(a2a.send("quero uma sala bonita"))
    second = task_of(a2a.send("quero uma sala bonita"))
    assert first["status"]["state"] == "TASK_STATE_REJECTED"
    assert text_of(first["status"]["message"]) == text_of(second["status"]["message"])
    assert text_of(first["status"]["message"]).startswith("Comando invalido: ")
    assert "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>" in text_of(first["status"]["message"])
    assert first["id"] != second["id"]


def test_a_rejected_command_never_reaches_the_mcp_server(stack, a2a):
    marker = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
    a2a.send("reservar sala=so-a-sala", traceparent=marker)
    assert marker.split("-")[1] not in stack.mcp_server.stderr_text()


# --- terminal tasks --------------------------------------------------------------------------------------------------


def test_a_terminal_task_refuses_every_continuation_and_is_left_untouched(a2a):
    for text in (booking_text("sala-porao", new_day(), 16, 17), booking_text("sala-delorean", new_day(), 9, 10), "oi"):
        task = task_of(a2a.send(text))
        before = a2a.get(task["id"])["result"]
        refused = a2a.send("escolha=sala-fusca", task_id=task["id"])
        assert "result" not in refused and refused["error"]["code"] == -32004
        assert "terminal state" in refused["error"]["message"]
        assert a2a.get(task["id"])["result"] == before


def test_a_context_that_does_not_match_the_task_is_refused(a2a):
    task = task_of(a2a.send(booking_text("sala-porao", new_day(), 17, 18)))
    refused = a2a.send("oi", task_id=task["id"], context_id="ctx-outro")
    assert refused["error"]["code"] == -32602


# --- invalid requests ------------------------------------------------------------------------------------------------


def post_raw(stack, content: bytes, headers: dict | None = None) -> httpx.Response:
    return httpx.post(stack.rpc_url, content=content, headers={"Content-Type": "application/json", **(headers or {})},
                      timeout=30)


def test_a_body_that_is_not_json_is_a_parse_error(stack):
    response = post_raw(stack, b"{not json")
    assert response.json()["error"]["code"] == -32700


@pytest.mark.parametrize("body, code", [
    ({"jsonrpc": "2.0", "id": 1, "method": "NoSuchMethod", "params": {}}, -32601),
    ({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}, -32601),  # the v0.3 name is not served
    ({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}}, -32601),
    ({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"role": "ROLE_USER"}}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "messageId": "m", "role": "ROLE_USER", "parts": [{"text": "x"}], "taskId": 5}}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "SendMessage"}, -32602),
    ({"id": 1, "method": "GetTask", "params": {"id": "x"}}, -32600),
    ([], -32600),
])
def test_invalid_jsonrpc_requests_get_a_jsonrpc_error_and_never_a_task(stack, body, code):
    response = post_raw(stack, json.dumps(body).encode())
    payload = response.json()
    assert payload["error"]["code"] == code, payload
    assert "result" not in payload


def test_the_error_response_echoes_the_request_id(stack):
    body = {"jsonrpc": "2.0", "id": "abc-123", "method": "GetTask", "params": {"id": "task-000000000000"}}
    assert post_raw(stack, json.dumps(body).encode()).json()["id"] == "abc-123"


def test_a_client_that_asks_for_a2a_0_3_is_refused_but_one_that_names_nothing_gets_1_0(a2a):
    refused = a2a.send(booking_text("sala-porao", new_day(), 8, 9), headers={"A2A-Version": "0.3"})
    assert refused["error"]["code"] == -32009
    accepted = a2a.send(booking_text("sala-porao", new_day(), 8, 9), headers={"A2A-Version": "1.0"})
    assert state_of(accepted) == "TASK_STATE_COMPLETED"


def test_only_post_is_served_on_the_rpc_path(stack):
    assert httpx.get(stack.rpc_url, timeout=15).status_code == 405


# --- tracing ---------------------------------------------------------------------------------------------------------


def mcp_lines(stack, trace_id: str) -> list[dict]:
    lines = []
    for line in stack.mcp_server.stderr_text().splitlines():
        if trace_id in line:
            fields = dict(re.findall(r"(\w+)=(\S+)", line))
            lines.append(fields)
    return lines


def test_the_callers_trace_id_reaches_every_mcp_request_of_the_task_with_a_new_span_each(stack, a2a):
    trace_id = secrets.token_hex(16)
    parent_span = secrets.token_hex(8)
    a2a.send(booking_text("sala-porao", new_day(), 18, 19), traceparent=f"00-{trace_id}-{parent_span}-01")
    lines = mcp_lines(stack, trace_id)
    assert [line["method"] for line in lines] == ["tools/list", "resources/read", "tools/call"]
    spans = [line["traceparent"].split("-")[2] for line in lines]
    assert len(set(spans)) == 3 and parent_span not in spans
    assert all(line["traceparent"].startswith(f"00-{trace_id}-") for line in lines)


def test_without_a_traceparent_the_mcp_requests_carry_none(stack, a2a):
    before = stack.mcp_server.stderr_text().count("traceparent=-")
    a2a.send(booking_text("sala-porao", new_day(), 19, 20))
    assert stack.mcp_server.stderr_text().count("traceparent=-") == before + 3


def test_an_invalid_traceparent_is_dropped_not_forwarded(stack, a2a):
    before = stack.mcp_server.stderr_text().count("traceparent=-")
    a2a.send(booking_text("sala-porao", new_day(), 8, 9), traceparent="not-a-traceparent")
    assert stack.mcp_server.stderr_text().count("traceparent=-") == before + 3
    assert "not-a-traceparent" not in stack.mcp_server.stderr_text()


def test_concurrent_tasks_keep_their_own_trace_ids(stack):
    traces = [secrets.token_hex(16) for _ in range(6)]
    results: dict[str, dict] = {}

    def run(index: int, trace_id: str) -> None:
        client = A2aClient(stack.rpc_url)
        room = ("sala-porao", "sala-aquario", "sala-fusca")[index % 3]
        results[trace_id] = client.send(booking_text(room, day, 8 + index, 9 + index, f"Pessoa{index}"),
                                        traceparent=f"00-{trace_id}-{secrets.token_hex(8)}-01")

    day = new_day()
    threads = [threading.Thread(target=run, args=(i, t)) for i, t in enumerate(traces)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for index, trace_id in enumerate(traces):
        task = task_of(results[trace_id])
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        lines = mcp_lines(stack, trace_id)
        assert [line["method"] for line in lines] == ["tools/list", "resources/read", "tools/call"]
        assert json.loads(text_of(task["artifacts"][0]))["responsavel"] == f"Pessoa{index}"


# --- independent tasks ----------------------------------------------------------------------------------------------


def test_two_tasks_are_independent_in_ids_artifacts_history_and_outcome(a2a):
    day = new_day()
    good = task_of(a2a.send(booking_text("sala-porao", day, 9, 10, "Lorraine")))
    bad = task_of(a2a.send(booking_text("sala-delorean", day, 9, 10, "George")))
    assert good["status"]["state"] == "TASK_STATE_COMPLETED" and bad["status"]["state"] == "TASK_STATE_FAILED"
    assert good["id"] != bad["id"] and good["contextId"] != bad["contextId"]
    again_good, again_bad = a2a.get(good["id"])["result"], a2a.get(bad["id"])["result"]
    assert json.loads(text_of(again_good["artifacts"][0]))["responsavel"] == "Lorraine"
    assert not again_bad.get("artifacts")
    assert all("George" not in text_of(m) for m in again_good["history"])
    assert all("Lorraine" not in text_of(m) for m in again_bad["history"])


def test_the_agent_keeps_serving_after_a_failed_task_with_the_same_mcp_connection(stack, a2a):
    a2a.send(booking_text("sala-delorean", new_day(), 9, 10))
    assert state_of(a2a.send(booking_text("sala-porao", new_day(), 10, 11))) == "TASK_STATE_COMPLETED"
    ids = [int(m) for m in re.findall(r"method=\S+ id=(\d+)", stack.mcp_server.stderr_text())]
    assert len(ids) == len(set(ids)), "JSON-RPC ids of the long-lived MCP client must never repeat"


# --- hygiene ---------------------------------------------------------------------------------------------------------


def test_no_a2a_response_carries_the_request_state_or_a_secret(stack, a2a):
    day = new_day()
    a2a.send(booking_text("sala-garagem", day, 14, 15, "Ocupante"))
    a2a.send(booking_text("sala-garagem", day, 14, 15, "Marty"))
    joined = "\n".join(a2a.bodies)
    assert "requestState" not in joined and stack.mcp_server.secret not in joined
    assert "7301" not in joined and "/mcp" not in joined


def test_the_agent_never_logs_the_request_state(stack, a2a):
    day = new_day()
    a2a.send(booking_text("sala-garagem", day, 15, 16, "Ocupante"))
    a2a.send(booking_text("sala-garagem", day, 15, 16, "Marty"))
    assert "requestState" not in stack.agent.stderr_text()
