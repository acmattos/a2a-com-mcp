"""Shared helpers for the multi-round-trip tests: build conflicts, answer them, inspect the outcome."""

import json
from datetime import date, timedelta

from conftest import CAPABILITIES_KEY, ELICITATION_FORM, PROTOCOL, VERSION_KEY  # noqa: F401

QUESTION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
ERR_NO_ALTERNATIVES = "Sem alternativas disponiveis no intervalo"
POLICY = "2026-11-01"


def at(day: int, hour: int, minute: int = 0) -> str:
    """``day`` counts from 2026-11-01, so day 3 is the seed reservation's date and later days roll over months."""
    date_part = date(2026, 11, 1) + timedelta(days=day - 1)
    return f"{date_part.isoformat()}T{hour:02d}:{minute:02d}:00-03:00"


def args(room="sala-garagem", day=3, start=14, end=15, owner="Marty") -> dict:
    return {"sala": room, "inicio": at(day, start), "fim": at(day, end), "responsavel": owner}


def text_of(result: dict) -> str:
    return " ".join(block.get("text", "") for block in result.get("content", []))


def choices_of(request: dict) -> list[str]:
    field = request["params"]["requestedSchema"]["properties"]["sala"]
    return field["enum"] if "enum" in field else [field["const"]]


class Pending:
    """One unanswered elicitation: what the client holds between the two rounds."""

    def __init__(self, arguments: dict, result: dict, request_id: object = None) -> None:
        self.arguments = arguments
        self.result = result
        self.request_id = request_id
        [(self.key, self.request)] = result["inputRequests"].items()
        self.state = result["requestState"]
        self.choices = choices_of(self.request)


def reserve(mcp, **kw) -> dict:
    return mcp.tool_result("reservar_sala", args(**kw))


def occupy(mcp, room, day, start=14, end=15, owner="Ocupante") -> None:
    result = reserve(mcp, room=room, day=day, start=start, end=end, owner=owner)
    assert result["isError"] is False, result


def ask(mcp, **kw) -> Pending:
    arguments = args(**kw)
    body = mcp.tool("reservar_sala", arguments)
    assert body["result"]["resultType"] == "input_required", body
    return Pending(arguments, body["result"], body["id"])


def conflict(mcp, day, room="sala-garagem", owner="Marty") -> Pending:
    occupy(mcp, room, day)
    return ask(mcp, room=room, day=day, owner=owner)


def respond(mcp, pending: Pending, response, *, key=None, state=None, arguments=None, tool="reservar_sala") -> dict:
    params = {"name": tool, "arguments": arguments or pending.arguments,
              "inputResponses": {key or pending.key: response},
              "requestState": state if state is not None else pending.state}
    status, body, _ = mcp.call("tools/call", params)
    return body


def accept(room: str) -> dict:
    return {"action": "accept", "content": {"sala": room}}


def is_free(mcp, room, day, start=14, end=15) -> bool:
    availability = mcp.tool_result("consultar_disponibilidade", args(room=room, day=day, start=start, end=end))
    return availability["structuredContent"]["livre"]


def holders(mcp, room, day, start=14, end=15) -> list[str]:
    availability = mcp.tool_result("consultar_disponibilidade", args(room=room, day=day, start=start, end=end))
    return [c["responsavel"] for c in availability["structuredContent"]["conflitos"]]


def flipped(token: str, index: int) -> str:
    return token[:index] + ("A" if token[index] != "A" else "B") + token[index + 1:]
