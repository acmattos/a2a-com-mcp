import pytest

from mcp_host import BookingRequest, InputRequired
from pending import PendingStore, PendingTask


def pause(tag: str) -> InputRequired:
    return InputRequired(
        request=BookingRequest("sala-a", "i", "f", tag), key=f"key-{tag}", message="m", choices=("x", "y"),
        input_requests={f"key-{tag}": {}}, policy_version="2026-11-01", request_state=f"STATE-{tag}",
    )


def entry(tag: str, trace: str | None = None) -> PendingTask:
    return PendingTask(pause(tag), trace)


def test_an_entry_belongs_to_exactly_one_task():
    store = PendingStore()
    store.put("task-1", entry("one"))
    store.put("task-2", entry("two"))
    assert store.get("task-1").pause.request_state == "STATE-one"
    assert store.get("task-2").pause.request_state == "STATE-two"
    assert len(store) == 2 and "task-1" in store and "task-3" not in store


def test_get_does_not_consume_but_claim_does_and_only_once():
    store = PendingStore()
    store.put("task-1", entry("one"))
    assert store.get("task-1") is store.get("task-1")
    first, second = store.claim("task-1"), store.claim("task-1")
    assert first.pause.request_state == "STATE-one" and second is None
    assert len(store) == 0 and store.get("task-1") is None


def test_claiming_one_task_leaves_the_others_untouched():
    store = PendingStore()
    store.put("task-1", entry("one"))
    store.put("task-2", entry("two"))
    store.claim("task-1")
    assert store.get("task-2").pause.request_state == "STATE-two"


def test_put_replaces_only_that_tasks_entry():
    store = PendingStore()
    store.put("task-1", entry("old"))
    store.put("task-2", entry("other"))
    store.put("task-1", entry("new"))
    assert store.get("task-1").pause.request_state == "STATE-new"
    assert store.get("task-2").pause.request_state == "STATE-other" and len(store) == 2


def test_discard_is_idempotent():
    store = PendingStore()
    store.put("task-1", entry("one"))
    store.discard("task-1")
    store.discard("task-1")
    assert len(store) == 0


def test_two_stores_share_nothing():
    first, second = PendingStore(), PendingStore()
    first.put("task-1", entry("one"))
    assert second.get("task-1") is None and len(second) == 0


def test_the_state_token_never_shows_in_repr_or_str():
    item = entry("secret")
    store = PendingStore()
    store.put("task-1", item)
    for rendered in (repr(item), str(item), repr(store), str(store)):
        assert "STATE-secret" not in rendered


def test_the_entry_keeps_the_initial_trace_context():
    assert entry("one", "00-" + "a" * 32 + "-" + "b" * 16 + "-01").traceparent.startswith("00-aaaa")


def test_a_second_claim_of_the_same_task_never_hands_over_another_tasks_entry():
    store = PendingStore()
    store.put("task-1", entry("one"))
    store.put("task-2", entry("two"))
    assert store.claim("task-1") is not None
    assert store.claim("task-1") is None and store.claim("task-3") is None
    assert store.get("task-2").pause.request_state == "STATE-two" and len(store) == 1
