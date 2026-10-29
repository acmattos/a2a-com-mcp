"""The bridge over real HTTP: real agent process, real MCP server process, plain httpx on the A2A side."""

import json
import re
import secrets
import threading

import pytest

from a2a_helpers import A2aClient, booking_text, new_day, state_of, task_of, text_of

POLICY = "2026-11-01"
WAIT = "alternativas: sala-fusca, sala-mirante"


@pytest.fixture
def a2a(stack):
    return A2aClient(stack.rpc_url)


def conflict(a2a, day, hour=14, owner="Marty", **kw):
    """Occupy sala-garagem (once per day/hour) and ask for it again: the second request pauses."""
    if not hasattr(conflict, "seen"):
        conflict.seen = set()
    if (day, hour) not in conflict.seen:
        conflict.seen.add((day, hour))
        assert state_of(a2a.send(booking_text("sala-garagem", day, hour, hour + 1, "Ocupante"))) == "TASK_STATE_COMPLETED"
    return a2a.send(booking_text("sala-garagem", day, hour, hour + 1, owner), **kw)


def mcp_lines(stack, trace_id: str) -> list[dict]:
    return [dict(re.findall(r"(\w+)=(\S+)", line)) for line in stack.mcp_server.stderr_text().splitlines()
            if trace_id in line]


def new_trace() -> tuple[str, str]:
    trace_id = secrets.token_hex(16)
    return trace_id, f"00-{trace_id}-{secrets.token_hex(8)}-01"


# --- pause ------------------------------------------------------------------------------------------------------------


def test_a_conflict_pauses_the_task_with_the_exact_alternatives_line(a2a):
    response = conflict(a2a, new_day())
    task = task_of(response)
    assert response["jsonrpc"] == "2.0" and task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert text_of(task["status"]["message"]) == WAIT
    assert task["status"]["message"]["parts"] == [{"text": WAIT}] and task["status"]["message"]["role"] == "ROLE_AGENT"
    assert not task.get("artifacts") and re.fullmatch(r"task-[0-9a-f]{12}", task["id"])


def test_get_task_keeps_returning_the_pause(a2a):
    sent = task_of(conflict(a2a, new_day()))
    for _ in range(2):
        got = a2a.get(sent["id"])["result"]
        assert got["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and text_of(got["status"]["message"]) == WAIT
        assert got["contextId"] == sent["contextId"] and got["history"] == sent["history"]


def test_the_same_conflict_twice_gives_byte_identical_pauses(a2a):
    day = new_day()
    first, second = task_of(conflict(a2a, day, owner="Um")), task_of(conflict(a2a, day, owner="Dois"))
    assert json.dumps(first["status"]["message"]["parts"]) == json.dumps(second["status"]["message"]["parts"])
    assert first["id"] != second["id"] and first["contextId"] != second["contextId"]


# --- answers ----------------------------------------------------------------------------------------------------------


def test_a_valid_choice_completes_the_same_task_with_the_chosen_room_and_the_policy(a2a):
    day = new_day()
    paused = task_of(conflict(a2a, day, owner="Marty"))
    done = task_of(a2a.send("escolha=sala-mirante", task_id=paused["id"]))
    assert done["id"] == paused["id"] and done["contextId"] == paused["contextId"]
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    [artifact] = done["artifacts"]
    reservation = json.loads(text_of(artifact))
    assert artifact["name"] == "reserva" and reservation["sala"] == "sala-mirante"
    assert reservation["politica"] == POLICY and reservation["responsavel"] == "Marty"
    assert reservation["inicio"].startswith(day.isoformat())
    assert a2a.get(paused["id"])["result"] == done


def test_the_history_is_the_whole_conversation_in_order(a2a):
    paused = task_of(conflict(a2a, new_day(), owner="Biff"))
    done = task_of(a2a.send("escolha=sala-fusca", task_id=paused["id"]))
    conversation = [(m["role"], text_of(m)) for m in done["history"]]
    assert [c[0] for c in conversation] == ["ROLE_USER", "ROLE_AGENT", "ROLE_USER", "ROLE_AGENT"]
    assert conversation[1][1] == WAIT and conversation[2][1] == "escolha=sala-fusca"
    assert re.fullmatch(r"Reserva res-\d+ confirmada na sala-fusca\.", conversation[3][1])
    assert {m["taskId"] for m in done["history"][1:]} == {paused["id"]}
    assert len({m["messageId"] for m in done["history"]}) == 4


def test_an_invalid_choice_keeps_the_task_paused_and_the_server_is_not_called(stack, a2a):
    paused = task_of(conflict(a2a, new_day()))
    calls_before = stack.mcp_server.stderr_text().count("method=tools/call")
    for text in ("escolha=sala-aquario", "escolha=sala-garagem", "escolha=", "sala-fusca", "escolha=SALA-FUSCA"):
        again = task_of(a2a.send(text, task_id=paused["id"]))
        assert again["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and text_of(again["status"]["message"]) == WAIT
    assert stack.mcp_server.stderr_text().count("method=tools/call") == calls_before
    assert state_of(a2a.send("escolha=sala-fusca", task_id=paused["id"])) == "TASK_STATE_COMPLETED"


def test_declining_cancels_the_task_creates_no_reservation_and_frees_nothing_wrongly(a2a):
    day = new_day()
    paused = task_of(conflict(a2a, day, owner="Biff"))
    canceled = task_of(a2a.send("escolha=recusar", task_id=paused["id"]))
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED" and not canceled.get("artifacts")
    assert canceled["contextId"] == paused["contextId"]
    # nothing was booked: both alternatives are still free, and the original room is still taken
    assert state_of(a2a.send(booking_text("sala-fusca", day, 14, 15, "Teste"))) == "TASK_STATE_COMPLETED"
    assert state_of(a2a.send(booking_text("sala-mirante", day, 14, 15, "Teste"))) == "TASK_STATE_COMPLETED"
    assert state_of(a2a.send(booking_text("sala-garagem", day, 14, 15, "Teste"))) == "TASK_STATE_FAILED"


def test_a_finished_task_refuses_any_further_answer_and_stays_untouched(a2a):
    day = new_day()
    for hour, answer in ((14, "escolha=sala-fusca"), (15, "escolha=recusar")):
        paused = task_of(conflict(a2a, day, hour=hour))
        task_of(a2a.send(answer, task_id=paused["id"]))
        before = a2a.get(paused["id"])["result"]
        refused = a2a.send("escolha=sala-mirante", task_id=paused["id"])
        assert refused["error"]["code"] == -32004 and "terminal state" in refused["error"]["message"]
        assert a2a.get(paused["id"])["result"] == before


def test_a_context_id_in_the_continuation_must_match_the_tasks(a2a):
    paused = task_of(conflict(a2a, new_day()))
    refused = a2a.send("escolha=sala-fusca", task_id=paused["id"], context_id="ctx-outro")
    assert refused["error"]["code"] == -32602
    assert a2a.get(paused["id"])["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert state_of(a2a.send("escolha=sala-fusca", task_id=paused["id"], context_id=paused["contextId"])) \
        == "TASK_STATE_COMPLETED"


# --- retry on the wire -----------------------------------------------------------------------------------------------


def test_the_retry_goes_out_with_a_new_jsonrpc_id_after_the_first_call_and_without_a_new_discovery(stack, a2a):
    trace_id, traceparent = new_trace()
    paused = task_of(conflict(a2a, new_day(), traceparent=traceparent))
    a2a.send("escolha=sala-fusca", task_id=paused["id"])
    lines = mcp_lines(stack, trace_id)
    assert [(line["method"]) for line in lines] == ["tools/list", "resources/read", "tools/call", "tools/call"]
    ids = [int(line["id"]) for line in lines]
    assert len(set(ids)) == 4 and ids == sorted(ids)
    first_call, retry = lines[2], lines[3]
    assert first_call["name"] == retry["name"] == "reservar_sala" and int(retry["id"]) > int(first_call["id"])


def test_the_continuation_keeps_the_trace_id_without_sending_a_traceparent(stack, a2a):
    trace_id, traceparent = new_trace()
    paused = task_of(conflict(a2a, new_day(), traceparent=traceparent))
    a2a.send("escolha=sala-mirante", task_id=paused["id"])  # no traceparent here
    lines = mcp_lines(stack, trace_id)
    assert len(lines) == 4
    assert all(line["traceparent"].startswith(f"00-{trace_id}-") for line in lines)
    assert len({line["traceparent"].split("-")[2] for line in lines}) == 4


def test_a_continuation_with_a_different_trace_id_cannot_move_the_task_to_another_trace(stack, a2a):
    trace_id, traceparent = new_trace()
    other_id, other = new_trace()
    paused = task_of(conflict(a2a, new_day(), traceparent=traceparent))
    a2a.send("escolha=sala-mirante", task_id=paused["id"], traceparent=other)
    assert len(mcp_lines(stack, trace_id)) == 4 and mcp_lines(stack, other_id) == []


# --- second pause ----------------------------------------------------------------------------------------------------


def test_when_the_alternatives_changed_meanwhile_the_task_pauses_again_with_the_new_list(a2a):
    day = new_day()
    paused = task_of(conflict(a2a, day, owner="Lorraine"))
    assert text_of(paused["status"]["message"]) == WAIT
    # somebody takes sala-fusca in the meantime
    assert state_of(a2a.send(booking_text("sala-fusca", day, 14, 15, "Outro"))) == "TASK_STATE_COMPLETED"
    again = task_of(a2a.send("escolha=sala-fusca", task_id=paused["id"]))
    assert again["id"] == paused["id"] and again["contextId"] == paused["contextId"]
    assert again["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert text_of(again["status"]["message"]) == "alternativas: sala-mirante"
    assert not again.get("artifacts")
    # the old list is not valid any more; the new one is
    still = task_of(a2a.send("escolha=sala-fusca", task_id=paused["id"]))
    assert text_of(still["status"]["message"]) == "alternativas: sala-mirante"
    done = task_of(a2a.send("escolha=sala-mirante", task_id=paused["id"]))
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert json.loads(text_of(done["artifacts"][0]))["sala"] == "sala-mirante"


# --- isolation --------------------------------------------------------------------------------------------------------


def test_two_tasks_paused_at_once_finish_each_with_its_own_room_when_answered_in_reverse(stack, a2a):
    day = new_day()
    (trace_a, tp_a), (trace_b, tp_b) = new_trace(), new_trace()
    # different hours: one answer must not change the other Task's alternatives (that is the second-pause case)
    one = task_of(conflict(a2a, day, hour=14, owner="George", traceparent=tp_a))
    two = task_of(conflict(a2a, day, hour=16, owner="Lorraine", traceparent=tp_b))
    assert one["id"] != two["id"] and one["contextId"] != two["contextId"]
    assert state_of({"result": {"task": a2a.get(one["id"])["result"]}}) == "TASK_STATE_INPUT_REQUIRED"
    done_two = task_of(a2a.send("escolha=sala-mirante", task_id=two["id"]))
    still_one = a2a.get(one["id"])["result"]
    assert still_one["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and text_of(still_one["status"]["message"]) == WAIT
    done_one = task_of(a2a.send("escolha=sala-fusca", task_id=one["id"]))
    reservation_one, reservation_two = (json.loads(text_of(t["artifacts"][0])) for t in (done_one, done_two))
    assert (reservation_one["sala"], reservation_one["responsavel"]) == ("sala-fusca", "George")
    assert (reservation_two["sala"], reservation_two["responsavel"]) == ("sala-mirante", "Lorraine")
    assert reservation_one["inicio"] != reservation_two["inicio"]
    assert reservation_one["reserva"] != reservation_two["reserva"]
    for trace_id in (trace_a, trace_b):
        assert len(mcp_lines(stack, trace_id)) == 4
    assert "George" not in json.dumps(done_two) and "Lorraine" not in json.dumps(done_one)


def test_two_simultaneous_answers_to_the_same_task_book_exactly_once(stack):
    day = new_day()
    client = A2aClient(stack.rpc_url)
    paused = task_of(conflict(client, day, owner="Marty"))
    calls_before = stack.mcp_server.stderr_text().count("method=tools/call")
    results: list[dict] = []

    def answer(room):
        results.append(A2aClient(stack.rpc_url).send(f"escolha={room}", task_id=paused["id"]))

    threads = [threading.Thread(target=answer, args=(room,)) for room in ("sala-fusca", "sala-mirante", "sala-fusca")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winners = [r for r in results if "result" in r]
    losers = [r for r in results if "error" in r]
    assert len(winners) == 1 and len(losers) == 2, results
    assert all(r["error"]["code"] == -32004 for r in losers)
    assert stack.mcp_server.stderr_text().count("method=tools/call") == calls_before + 1
    final = client.get(paused["id"])["result"]
    assert final["status"]["state"] == "TASK_STATE_COMPLETED" and len(final["artifacts"]) == 1
    # one reservation only: the room the winner chose is taken, the other one is still free
    taken = json.loads(text_of(final["artifacts"][0]))["sala"]
    free = ({"sala-fusca", "sala-mirante"} - {taken}).pop()
    assert state_of(client.send(booking_text(taken, day, 14, 15, "Teste"))) != "TASK_STATE_COMPLETED"
    assert state_of(client.send(booking_text(free, day, 14, 15, "Teste"))) == "TASK_STATE_COMPLETED"


# --- no leak ----------------------------------------------------------------------------------------------------------


def test_no_a2a_response_and_no_agent_log_carries_a_request_state(stack):
    client = A2aClient(stack.rpc_url)
    day = new_day()
    paused = task_of(conflict(client, day, hour=17, owner="Marty"))
    client.get(paused["id"])
    client.send("escolha=sala-inexistente", task_id=paused["id"])
    client.send("escolha=sala-fusca", task_id=paused["id"])
    other = task_of(conflict(client, day, hour=18, owner="Biff"))
    client.send("escolha=recusar", task_id=other["id"])
    joined = "\n".join(client.bodies)
    assert "requestState" not in joined and "inputRequests" not in joined and "resultType" not in joined
    assert not re.search(r"[A-Za-z0-9_\-]{60,}", joined), "a long opaque string reached the A2A client"
    assert stack.mcp_server.secret not in joined
    assert "requestState" not in stack.agent.stderr_text() and not re.search(r"[A-Za-z0-9_\-]{60,}", stack.agent.stderr_text())
