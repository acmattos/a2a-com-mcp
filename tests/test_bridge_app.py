"""The A2A <-> MRTR bridge against a scripted MCP server: exact echo of key/state, isolation, cleanup, races."""

import datetime
import json
import re
import secrets
import threading

import httpx2
import pytest
from starlette.testclient import TestClient

from a2a_helpers import booking_text, state_of, task_of, text_of
from agent_app import create_app
from bridge_stub import BridgeStub
from mcp_host import McpHost
from pending import PendingStore
from test_a2a_app import Client

DAY = datetime.date(2027, 5, 1)
WAIT = "alternativas: sala-a, sala-b"


def trace_of(request: dict) -> str:
    return re.search(r"00-([0-9a-f]{32})-", request["params"]["_meta"]["traceparent"]).group(1)


@pytest.fixture
def bridge():
    opened = []

    def start(stub):
        store = PendingStore()
        app = create_app(mcp_url="http://stub/mcp", host_factory=lambda url: McpHost(url, http_transport=stub),
                         pending=store)
        http = TestClient(app)
        http.__enter__()
        opened.append(http)
        return Client(http), store

    yield start
    for http in reversed(opened):
        http.__exit__(None, None, None)


def book(client, tag, hour=9, **kw):
    return client.send(booking_text("sala-x", DAY, hour, hour + 1, f"Conf{tag}"), **kw)


# --- the pause --------------------------------------------------------------------------------------------------------


def test_a_conflict_pauses_the_task_with_exactly_the_alternatives_line_in_enum_order(bridge):
    client, store = bridge(BridgeStub())
    task = task_of(book(client, "a"))
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert text_of(task["status"]["message"]) == WAIT
    assert task["status"]["message"]["role"] == "ROLE_AGENT" and len(task["status"]["message"]["parts"]) == 1
    # the SDK moves a status message into history when the Task is continued (see the completed-task test)
    assert [text_of(m) for m in task["history"]] == [booking_text("sala-x", DAY, 9, 10, "Confa")]
    assert not task.get("artifacts") and task["id"] in store


def test_the_alternatives_follow_the_enum_order_whatever_it_is(bridge):
    class Reordered(BridgeStub):
        async def _handle(self, request):
            response = await super()._handle(request)
            body = json.loads(response.content)
            asked = body["result"].get("inputRequests", {}).get("escolha_de_sala")
            if asked:
                asked["params"]["requestedSchema"]["properties"]["sala"]["enum"] = ["sala-z", "sala-b", "sala-a"]
            return httpx2.Response(200, json=body)

    client, _ = bridge(Reordered())
    assert text_of(task_of(book(client, "a"))["status"]["message"]) == "alternativas: sala-z, sala-b, sala-a"


def test_two_equal_pauses_are_byte_identical(bridge):
    client, _ = bridge(BridgeStub())
    first, second = task_of(book(client, "a")), task_of(book(client, "a"))
    assert json.dumps(first["status"]["message"]["parts"]) == json.dumps(second["status"]["message"]["parts"])
    assert first["id"] != second["id"]


def test_get_task_shows_the_pause_and_does_not_disturb_it(bridge):
    client, store = bridge(BridgeStub())
    sent = task_of(book(client, "a"))
    for _ in range(3):
        got = client.get(sent["id"])["result"]
        assert got["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and text_of(got["status"]["message"]) == WAIT
        assert got["history"] == sent["history"] and got["contextId"] == sent["contextId"]
    assert sent["id"] in store


def test_the_private_entry_holds_everything_the_retry_needs(bridge):
    stub = BridgeStub()
    client, store = bridge(stub)
    trace = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
    task = task_of(book(client, "a", traceparent=trace))
    entry = store.get(task["id"])
    assert entry.pause.key == "escolha_de_sala" and entry.pause.choices == ("sala-a", "sala-b")
    assert entry.pause.request_state == stub.issued["a"]
    assert entry.pause.request.owner == "Confa" and entry.pause.policy_version == "stub-policy"
    assert entry.traceparent == trace


# --- the continuation ------------------------------------------------------------------------------------------------


def test_a_valid_choice_resumes_with_the_same_key_the_untouched_state_the_original_arguments_and_a_new_id(bridge):
    stub = BridgeStub()
    client, store = bridge(stub)
    paused = task_of(book(client, "a"))
    first_call = [r for r in stub.requests if r["method"] == "tools/call"][0]
    done = task_of(client.send("escolha=sala-b", task_id=paused["id"]))
    [retry] = stub.retries
    assert not stub.bad_retries
    assert retry["params"]["requestState"] == stub.issued["a"]
    assert retry["params"]["inputResponses"] == {"escolha_de_sala": {"action": "accept", "content": {"sala": "sala-b"}}}
    assert retry["params"]["arguments"] == first_call["params"]["arguments"]
    assert retry["id"] != first_call["id"] and retry["id"] > first_call["id"]
    assert done["status"]["state"] == "TASK_STATE_COMPLETED" and done["id"] == paused["id"]
    assert done["contextId"] == paused["contextId"]
    assert paused["id"] not in store


def test_the_completed_task_keeps_the_whole_conversation_in_order_and_the_artifact(bridge):
    client, _ = bridge(BridgeStub())
    paused = task_of(book(client, "a"))
    done = task_of(client.send("escolha=sala-b", task_id=paused["id"]))
    conversation = [(m["role"], text_of(m)) for m in done["history"]]
    assert conversation[:3] == [("ROLE_USER", booking_text("sala-x", DAY, 9, 10, "Confa")),
                                ("ROLE_AGENT", WAIT), ("ROLE_USER", "escolha=sala-b")]
    assert re.fullmatch(r"Reserva res-\d+ confirmada na sala-b\.", conversation[3][1]) and len(conversation) == 4
    [artifact] = done["artifacts"]
    reservation = json.loads(text_of(artifact))
    assert artifact["name"] == "reserva" and reservation["sala"] == "sala-b" and reservation["politica"] == "stub-policy"
    assert client.get(paused["id"])["result"] == done


def test_an_invalid_choice_keeps_the_task_paused_repeats_the_list_and_never_calls_mcp(bridge):
    stub = BridgeStub()
    client, store = bridge(stub)
    paused = task_of(book(client, "a"))
    calls = len(stub.requests)
    for text in ("escolha=sala-inexistente", "escolha=", "sala-a", "qualquer coisa", "escolha=sala-a sala-b", "RECUSAR"):
        again = task_of(client.send(text, task_id=paused["id"]))
        assert again["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and text_of(again["status"]["message"]) == WAIT
    assert len(stub.requests) == calls and paused["id"] in store
    assert task_of(client.send("escolha=sala-a", task_id=paused["id"]))["status"]["state"] == "TASK_STATE_COMPLETED"
    assert not stub.bad_retries


def test_declining_cancels_the_task_without_artifact_and_sends_decline_with_the_same_state(bridge):
    stub = BridgeStub()
    client, store = bridge(stub)
    paused = task_of(book(client, "a"))
    canceled = task_of(client.send("escolha=recusar", task_id=paused["id"]))
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED" and not canceled.get("artifacts")
    [retry] = stub.retries
    assert retry["params"]["inputResponses"] == {"escolha_de_sala": {"action": "decline"}}
    assert retry["params"]["requestState"] == stub.issued["a"] and not stub.bad_retries
    assert paused["id"] not in store
    assert text_of(canceled["status"]["message"]) in [text_of(m) for m in canceled["history"]]


def test_a_tool_error_on_the_retry_fails_the_task_with_the_literal_message(bridge):
    client, store = bridge(BridgeStub(fail={"a"}))
    paused = task_of(book(client, "a"))
    failed = task_of(client.send("escolha=sala-a", task_id=paused["id"]))
    assert failed["status"]["state"] == "TASK_STATE_FAILED" and text_of(failed["status"]["message"]) == "falhou a"
    assert not failed.get("artifacts") and paused["id"] not in store


def test_a_second_pause_replaces_the_state_of_the_same_task_and_can_still_be_answered(bridge):
    stub = BridgeStub(repause={"a"})
    client, store = bridge(stub)
    paused = task_of(book(client, "a"))
    second = task_of(client.send("escolha=sala-a", task_id=paused["id"]))
    assert second["id"] == paused["id"] and second["contextId"] == paused["contextId"]
    assert second["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert text_of(second["status"]["message"]) == "alternativas: sala-z"
    assert len(store) == 1 and store.get(paused["id"]).pause.request_state == "TOKEN2-a"
    # the old choice list is gone: sala-a is no longer valid, sala-z is
    stale = task_of(client.send("escolha=sala-a", task_id=paused["id"]))
    assert text_of(stale["status"]["message"]) == "alternativas: sala-z"
    done = task_of(client.send("escolha=sala-z", task_id=paused["id"]))
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert json.loads(text_of(done["artifacts"][0]))["sala"] == "sala-z"
    assert [r["params"]["requestState"] for r in stub.retries] == ["TOKEN-a-1", "TOKEN2-a"] and not stub.bad_retries
    assert len(store) == 0


# --- isolation -------------------------------------------------------------------------------------------------------


def test_two_paused_tasks_answered_in_reverse_order_keep_their_own_state_and_rooms(bridge):
    stub = BridgeStub()
    client, store = bridge(stub)
    one, two = task_of(book(client, "1", 9)), task_of(book(client, "2", 11))
    assert one["id"] != two["id"] and len(store) == 2
    assert store.get(one["id"]).pause.request_state != store.get(two["id"]).pause.request_state
    done_two = task_of(client.send("escolha=sala-b", task_id=two["id"]))
    assert one["id"] in store and two["id"] not in store
    done_one = task_of(client.send("escolha=sala-a", task_id=one["id"]))
    reservations = {t["id"]: json.loads(text_of(t["artifacts"][0])) for t in (done_one, done_two)}
    assert reservations[one["id"]]["sala"] == "sala-a" and reservations[one["id"]]["responsavel"] == "Conf1"
    assert reservations[two["id"]]["sala"] == "sala-b" and reservations[two["id"]]["responsavel"] == "Conf2"
    assert not stub.bad_retries and len(store) == 0
    by_owner = {r["params"]["arguments"]["responsavel"]: r["params"]["requestState"] for r in stub.retries}
    assert by_owner["Conf2"].startswith("TOKEN-2-") and by_owner["Conf1"].startswith("TOKEN-1-")
    assert [r["params"]["arguments"]["responsavel"] for r in stub.retries] == ["Conf2", "Conf1"]


def test_a_choice_for_one_task_never_touches_another_paused_task(bridge):
    client, store = bridge(BridgeStub())
    one, two = task_of(book(client, "1", 9)), task_of(book(client, "2", 11))
    client.send("escolha=recusar", task_id=one["id"])
    still = client.get(two["id"])["result"]
    assert still["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and two["id"] in store


def test_two_simultaneous_continuations_of_the_same_task_consume_the_pause_once(bridge):
    stub = BridgeStub(delay=0.05)
    client, store = bridge(stub)
    paused = task_of(book(client, "a"))
    results: list[dict] = []

    def answer(room):
        results.append(Client(client._http).send(f"escolha={room}", task_id=paused["id"]))

    threads = [threading.Thread(target=answer, args=(room,)) for room in ("sala-a", "sala-b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winners = [r for r in results if "result" in r and state_of(r) == "TASK_STATE_COMPLETED"]
    losers = [r for r in results if "error" in r]
    assert len(winners) == 1 and len(losers) == 1, results
    assert losers[0]["error"]["code"] == -32004
    assert len(stub.retries) == 1 and not stub.bad_retries, "the pause must be consumed once"
    final = client.get(paused["id"])["result"]
    assert final["status"]["state"] == "TASK_STATE_COMPLETED" and len(final["artifacts"]) == 1
    assert paused["id"] not in store


def test_many_simultaneous_continuations_still_book_once(bridge):
    stub = BridgeStub(delay=0.02)
    client, _ = bridge(stub)
    paused = task_of(book(client, "a"))
    results = []
    threads = [threading.Thread(
        target=lambda: results.append(Client(client._http).send("escolha=sala-a", task_id=paused["id"])))
        for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for r in results if "result" in r) == 1 and len(stub.retries) == 1


# --- trace ----------------------------------------------------------------------------------------------------------


def test_the_continuation_keeps_the_trace_id_of_the_task_even_without_a_traceparent(bridge):
    stub = BridgeStub()
    client, _ = bridge(stub)
    trace = secrets.token_hex(16)
    paused = task_of(book(client, "a", traceparent=f"00-{trace}-{secrets.token_hex(8)}-01"))
    client.send("escolha=sala-a", task_id=paused["id"])
    calls = [r for r in stub.requests if r["method"] == "tools/call"]
    assert [trace_of(r) for r in calls] == [trace, trace]
    spans = {r["params"]["_meta"]["traceparent"].split("-")[2] for r in calls}
    assert len(spans) == 2


def test_a_continuation_that_brings_another_trace_id_does_not_change_the_tasks_trace(bridge):
    stub = BridgeStub()
    client, _ = bridge(stub)
    trace = secrets.token_hex(16)
    paused = task_of(book(client, "a", traceparent=f"00-{trace}-{secrets.token_hex(8)}-01"))
    other = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
    client.send("escolha=sala-a", task_id=paused["id"], traceparent=other)
    assert {trace_of(r) for r in stub.requests if r["method"] == "tools/call"} == {trace}


def test_a_task_without_a_trace_adopts_the_one_its_continuation_brings(bridge):
    stub = BridgeStub()
    client, _ = bridge(stub)
    paused = task_of(book(client, "a"))
    trace = secrets.token_hex(16)
    client.send("escolha=sala-a", task_id=paused["id"], traceparent=f"00-{trace}-{secrets.token_hex(8)}-01")
    assert trace_of(stub.retries[0]) == trace


def test_overlapping_paused_tasks_keep_separate_trace_ids_across_the_pause(bridge):
    stub = BridgeStub(delay=0.02)
    client, _ = bridge(stub)
    traces = {tag: secrets.token_hex(16) for tag in "123"}
    ids = {}
    for tag, trace in traces.items():
        ids[tag] = task_of(book(client, tag, 8 + int(tag), traceparent=f"00-{trace}-{secrets.token_hex(8)}-01"))["id"]
    threads = [threading.Thread(target=lambda t=tag: Client(client._http).send("escolha=sala-a", task_id=ids[t]))
               for tag in reversed(list(traces))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    for tag, trace in traces.items():
        retry = next(r for r in stub.retries if r["params"]["arguments"]["responsavel"] == f"Conf{tag}")
        assert trace_of(retry) == trace


# --- terminal states clean up and refuse -----------------------------------------------------------------------------


def test_every_way_a_task_ends_leaves_nothing_behind(bridge):
    stub = BridgeStub(fail={"f"})
    client, store = bridge(stub)
    ids = {tag: task_of(book(client, tag, 8 + i))["id"] for i, tag in enumerate("cdf")}
    assert len(store) == 3
    client.send("escolha=sala-a", task_id=ids["c"])
    client.send("escolha=recusar", task_id=ids["d"])
    client.send("escolha=sala-a", task_id=ids["f"])
    assert len(store) == 0
    client.send("nada a ver")  # rejected at the start
    client.send(booking_text("sala-x", DAY, 18, 19, "Livre"))  # completed without ever pausing
    assert len(store) == 0


def test_a_task_that_ended_refuses_further_answers_and_stays_as_it_was(bridge):
    client, _ = bridge(BridgeStub())
    for tag, answer in (("c", "escolha=sala-a"), ("d", "escolha=recusar")):
        paused = task_of(book(client, tag))
        task_of(client.send(answer, task_id=paused["id"]))
        before = client.get(paused["id"])["result"]
        refused = client.send("escolha=sala-b", task_id=paused["id"])
        assert refused["error"]["code"] == -32004 and "terminal state" in refused["error"]["message"]
        assert client.get(paused["id"])["result"] == before


def test_a_continuation_with_no_pending_entry_is_refused_not_guessed(bridge):
    client, store = bridge(BridgeStub())
    paused = task_of(book(client, "a"))
    store.discard(paused["id"])  # the agent lost the entry
    refused = client.send("escolha=sala-a", task_id=paused["id"])
    assert refused["error"]["code"] == -32004
    assert client.get(paused["id"])["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


def test_an_mcp_failure_on_the_retry_fails_the_task_and_drops_the_state(bridge):
    state = {"down": False}

    class Flaky(BridgeStub):
        async def _handle(self, request):
            if state["down"]:
                raise httpx2.ConnectError("boom")
            return await super()._handle(request)

    client, store = bridge(Flaky())
    paused = task_of(book(client, "a"))
    state["down"] = True
    failed = task_of(client.send("escolha=sala-a", task_id=paused["id"]))
    assert failed["status"]["state"] == "TASK_STATE_FAILED"
    assert text_of(failed["status"]["message"]) == "Servidor de salas indisponivel. Tente novamente."
    assert paused["id"] not in store


# --- no leak of the state --------------------------------------------------------------------------------------------


def test_no_a2a_body_ever_contains_a_state_token_in_any_step(bridge):
    stub = BridgeStub(repause={"b"})
    client, _ = bridge(stub)
    a, b = task_of(book(client, "a")), task_of(book(client, "b", 11))
    client.get(a["id"])
    client.send("escolha=sala-q", task_id=a["id"])
    client.send("escolha=sala-a", task_id=a["id"])
    client.send("escolha=sala-a", task_id=b["id"])
    client.send("escolha=sala-z", task_id=b["id"])
    client.get(b["id"])
    joined = "\n".join(client.bodies)
    assert "TOKEN" not in joined and "requestState" not in joined and "inputRequests" not in joined
    assert "escolha_de_sala" not in joined


def test_the_agent_logs_nothing_about_the_state(bridge, caplog, capsys):
    import logging

    caplog.set_level(logging.DEBUG)
    client, _ = bridge(BridgeStub(fail={"f"}))
    for tag in ("a", "f"):
        paused = task_of(book(client, tag))
        client.send("escolha=sala-a", task_id=paused["id"])
    captured = capsys.readouterr()
    leaking = {record.name for record in caplog.records if "TOKEN" in record.getMessage()}
    assert not leaking, f"these loggers mention the state at DEBUG: {leaking}"
    assert "TOKEN" not in captured.out + captured.err


def test_the_pause_is_stored_before_the_task_announces_it(bridge, monkeypatch):
    """An answer can arrive the instant the Task says INPUT_REQUIRED, so the entry must already be there."""
    import executor

    client, store = bridge(BridgeStub())
    seen = []
    real = executor._ask

    async def watching(updater, text):
        seen.append(len(store))
        await real(updater, text)

    monkeypatch.setattr(executor, "_ask", watching)
    book(client, "a")
    assert seen == [1]
