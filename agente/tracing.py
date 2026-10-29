"""W3C ``traceparent`` handling: keep the caller's trace-id, mint a new span-id for each hop.

Pure functions. The trace context is always passed explicitly, never kept in module state, so
concurrent tasks cannot see each other's trace.
"""

import re
import secrets
from dataclasses import dataclass

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_EMITTED_VERSION = "00"


@dataclass(frozen=True)
class TraceContext:
    version: str
    trace_id: str
    span_id: str
    flags: str


def parse_traceparent(value: object) -> TraceContext | None:
    """Return the parsed context, or ``None`` for anything that is not a valid traceparent."""
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT.match(value)
    if match is None:
        return None
    version, trace_id, span_id, flags = match.groups()
    if version == "ff" or set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    return TraceContext(version, trace_id, span_id, flags)


def child_of(traceparent: object) -> str | None:
    """A traceparent for the next hop: same trace-id and flags, new span-id. ``None`` if the input is invalid."""
    parent = parse_traceparent(traceparent)
    if parent is None:
        return None
    span_id = secrets.token_hex(8)
    while set(span_id) == {"0"}:
        span_id = secrets.token_hex(8)
    return f"{_EMITTED_VERSION}-{parent.trace_id}-{span_id}-{parent.flags}"
