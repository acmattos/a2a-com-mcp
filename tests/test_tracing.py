import re

import pytest

from tracing import child_of, parse_traceparent

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"
VALID = f"00-{TRACE_ID}-{SPAN_ID}-01"


def test_a_valid_traceparent_is_parsed_into_its_parts():
    context = parse_traceparent(VALID)
    assert (context.trace_id, context.span_id, context.flags) == (TRACE_ID, SPAN_ID, "01")


@pytest.mark.parametrize("value", [
    None,
    "",
    "garbage",
    f"00-{TRACE_ID}-{SPAN_ID}",  # missing flags
    f"00-{TRACE_ID}-{SPAN_ID}-01-extra",
    f"00-{TRACE_ID[:-1]}-{SPAN_ID}-01",  # short trace id
    f"00-{TRACE_ID}-{SPAN_ID[:-1]}-01",  # short span id
    f"00-{TRACE_ID.upper()}-{SPAN_ID}-01",  # W3C requires lowercase
    f"00-{'0' * 32}-{SPAN_ID}-01",  # all-zero trace id is invalid
    f"00-{TRACE_ID}-{'0' * 16}-01",  # all-zero span id is invalid
    f"ff-{TRACE_ID}-{SPAN_ID}-01",  # version ff is forbidden
    f"00-{TRACE_ID}-{SPAN_ID}-zz",
    42,
])
def test_invalid_traceparents_are_rejected_not_guessed(value):
    assert parse_traceparent(value) is None
    assert child_of(value) is None


def test_the_child_keeps_the_trace_id_and_flags_but_gets_a_new_span_id():
    child = child_of(VALID)
    context = parse_traceparent(child)
    assert context.trace_id == TRACE_ID and context.flags == "01"
    assert context.span_id != SPAN_ID
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", child)


def test_every_child_gets_its_own_span_id():
    spans = {parse_traceparent(child_of(VALID)).span_id for _ in range(50)}
    assert len(spans) == 50 and "0" * 16 not in spans


def test_an_unsampled_flag_is_preserved():
    assert child_of(f"00-{TRACE_ID}-{SPAN_ID}-00").endswith("-00")


def test_a_future_version_is_read_but_a_version_00_child_is_emitted():
    child = child_of(f"01-{TRACE_ID}-{SPAN_ID}-01")
    assert child.startswith("00-") and TRACE_ID in child
