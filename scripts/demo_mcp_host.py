"""Walk the MCP host through every path against a running room server and print what happened.

    uv run python scripts/demo_mcp_host.py [--url http://localhost:7301/mcp] [--traceparent 00-<32hex>-<16hex>-01]

Read the server's stderr next to this output: the same trace-id, ``tools/list`` before the first ``tools/call``,
and different JSON-RPC ids on the first call and on the retry. The requestState is never printed.
"""

import argparse
import asyncio
import random
import secrets
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agente"))

from mcp_host import (  # noqa: E402
    DEFAULT_URL, Answer, BookingOutcome, BookingRequest, InputRequired, McpHost, McpProtocolError, McpUnavailable,
)
from tracing import parse_traceparent  # noqa: E402


def describe(outcome: BookingOutcome) -> str:
    if isinstance(outcome, InputRequired):
        return (f"PAUSE   question={outcome.message!r} choices={list(outcome.choices)} key={outcome.key} "
                f"state=<opaque, {len(outcome.request_state)} chars>")
    return f"{type(outcome).__name__.upper():7s} {outcome}"


def request(room: str, day: date, start: int, end: int, owner: str) -> BookingRequest:
    stamp = lambda hour: f"{day.isoformat()}T{hour:02d}:00:00-03:00"  # noqa: E731
    return BookingRequest(room, stamp(start), stamp(end), owner)


async def demo(url: str, traceparent: str) -> None:
    # A random day in 2027 keeps reruns independent of the reservations earlier runs created.
    day = date(2027, 1, 1) + timedelta(days=random.randrange(0, 300))
    async with McpHost(url) as host:
        print(f"trace-id: {parse_traceparent(traceparent).trace_id}   day: {day}")

        print("\n1. free booking (tools/list -> resources/read -> tools/call)")
        print("  ", describe(await host.book(request("sala-aquario", day, 9, 10, "Doc"), traceparent=traceparent)))

        print("\n2. unknown room -> execution error")
        print("  ", describe(await host.book(request("sala-delorean", day, 9, 10, "Doc"), traceparent=traceparent)))

        print("\n3. conflict -> pause (the host answers nothing) -> resume with the caller's answer")
        await host.book(request("sala-garagem", day, 14, 15, "Ocupante"), traceparent=traceparent)
        pending = await host.book(request("sala-garagem", day, 14, 15, "Marty"), traceparent=traceparent)
        print("  ", describe(pending))
        assert isinstance(pending, InputRequired)
        print("  ", describe(await host.resume(pending, Answer.accept(pending.choices[0]), traceparent=traceparent)))

        print("\n4. conflict -> pause -> decline")
        pending = await host.book(request("sala-garagem", day, 14, 15, "Biff"), traceparent=traceparent)
        print("  ", describe(pending))
        assert isinstance(pending, InputRequired)
        print("  ", describe(await host.resume(pending, Answer.decline(), traceparent=traceparent)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--traceparent", default=f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01")
    args = parser.parse_args()
    if parse_traceparent(args.traceparent) is None:
        parser.error("--traceparent is not a valid W3C traceparent")
    try:
        asyncio.run(demo(args.url, args.traceparent))
    except McpUnavailable as error:
        print(f"server unavailable: {error}", file=sys.stderr)
        return 1
    except McpProtocolError as error:
        print(f"protocol error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
