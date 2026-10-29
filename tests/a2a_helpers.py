"""A2A over raw HTTP, written like the grader's client: JSON-RPC by hand, no SDK between the test and the wire."""

import itertools
import secrets
from datetime import date, timedelta

import httpx

_DAYS = itertools.count()
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


def new_day() -> date:
    """A day nobody booked yet in this module's server (each call hands out the next one)."""
    return date(2027, 3, 1) + timedelta(days=next(_DAYS))


def stamp(day: date, hour: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:00:00-03:00"


def booking_text(room: str, day: date, start: int, end: int, owner: str = "Doc") -> str:
    return f"reservar sala={room} inicio={stamp(day, start)} fim={stamp(day, end)} responsavel={owner}"


class A2aClient:
    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url
        self.bodies: list[str] = []

    def rpc(self, method: str, params: dict | None, *, traceparent: str | None = None,
            headers: dict | None = None, request_id: object = "auto") -> httpx.Response:
        body = {"jsonrpc": "2.0", "id": secrets.token_hex(4) if request_id == "auto" else request_id,
                "method": method, "params": params}
        sent = {"Content-Type": "application/json", **({"traceparent": traceparent} if traceparent else {}),
                **(headers or {})}
        response = httpx.post(self.rpc_url, json=body, headers=sent, timeout=60)
        self.bodies.append(response.text)
        return response

    def send(self, text: str, *, task_id: str | None = None, context_id: str | None = None,
             traceparent: str | None = None, message_id: str | None = None, headers: dict | None = None) -> dict:
        message = {"messageId": message_id or f"msg-{secrets.token_hex(6)}", "role": "ROLE_USER",
                   "parts": [{"text": text}]}
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        return self.rpc("SendMessage", {"message": message}, traceparent=traceparent, headers=headers).json()

    def get(self, task_id: str, **params) -> dict:
        return self.rpc("GetTask", {"id": task_id, **params}).json()


def task_of(response: dict) -> dict:
    return response["result"]["task"]


def state_of(response: dict) -> str:
    return task_of(response)["status"]["state"]


def text_of(message: dict | None) -> str:
    return " ".join(part.get("text", "") for part in (message or {}).get("parts", []))
