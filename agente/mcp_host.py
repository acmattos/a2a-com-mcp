"""The agent as an MCP host: a real client of the room server, over Streamable HTTP only.

It talks protocol and nothing else: no room, schedule or policy rule lives here. One booking is
``tools/list`` (and a check of what was discovered), ``resources/read`` of the policy, then ``tools/call``. When the
server answers ``input_required`` the host does NOT answer the elicitation: it hands the question and the opaque
``requestState`` back to the caller as an :class:`InputRequired` outcome, and a later :meth:`McpHost.resume` repeats
the call with the caller's answer, the state exactly as received and a new JSON-RPC id.

A single long-lived client serves every booking, so JSON-RPC ids are unique for the host's whole life. The trace
context is passed explicitly per call (never kept in shared state), so concurrent bookings cannot mix traces.
"""

import re
from collections.abc import Mapping
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, Implementation, InputRequiredResult, TextContent, Tool
from pydantic import ValidationError

from http_boundary import (
    UNREACHABLE_CODE,
    UNREACHABLE_ORIGIN,
    FormOnlyElicitationTransport,
    UnreachableAsErrorTransport,
)
from tracing import child_of

PROTOCOL_VERSION = "2026-07-28"
DEFAULT_URL = "http://localhost:7301/mcp"
DEFAULT_TIMEOUT_SECONDS = 30.0
BOOKING_TOOL = "reservar_sala"
POLICY_URI = "politica://uso"
CLIENT_INFO = Implementation(name="agente-central-de-salas", version="1.0.0")
MAX_TOOL_PAGES = 50

_POLICY_VERSION_LINE = re.compile(r"^versao:\s*(\S+)\s*$")


class McpHostError(Exception):
    """Base class of everything the host raises on purpose."""


class McpUnavailable(McpHostError):
    """The MCP server could not be reached. The message carries no URL and no internals."""


class ContractError(McpHostError):
    """The server answered something this host will not guess its way through."""


class McpProtocolError(McpHostError):
    """The server answered with a JSON-RPC error (for instance ``-32602`` for a rejected ``requestState``)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code, self.message, self.data = code, message, data


@dataclass(frozen=True)
class BookingRequest:
    room: str
    start: str
    end: str
    owner: str

    def arguments(self) -> dict[str, str]:
        return {"sala": self.room, "inicio": self.start, "fim": self.end, "responsavel": self.owner}


@dataclass(frozen=True)
class Answer:
    """What the caller answers to a paused booking."""

    action: Literal["accept", "decline"]
    room: str | None = None

    @classmethod
    def accept(cls, room: str) -> "Answer":
        return cls("accept", room)

    @classmethod
    def decline(cls) -> "Answer":
        return cls("decline")

    def to_wire(self) -> dict[str, Any]:
        if self.action == "accept":
            return {"action": "accept", "content": {"sala": self.room}}
        return {"action": "decline"}


@dataclass(frozen=True)
class Reserved:
    reservation: Mapping[str, Any]
    policy_version: str


@dataclass(frozen=True)
class Declined:
    reason: str
    policy_version: str


@dataclass(frozen=True)
class ToolFailed:
    """An execution error of the tool (``isError``). ``message`` is the tool's own text, unchanged."""

    message: str


@dataclass(frozen=True)
class InputRequired:
    """The server needs an answer. ``key``, ``input_requests`` and ``request_state`` are opaque and must be echoed."""

    request: BookingRequest
    key: str
    message: str
    choices: tuple[str, ...]
    input_requests: Mapping[str, Any]
    policy_version: str
    request_state: str = field(repr=False)


BookingOutcome = Reserved | Declined | ToolFailed | InputRequired


async def _refuse_elicitation(context: Any, params: Any) -> Any:
    """Declared only so the client advertises elicitation. The host forwards questions, it never answers them."""
    raise McpHostError("the host never answers an elicitation: it forwards the question to its caller")


class McpHost:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        http_transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url
        self._timeout = timeout
        boundary = FormOnlyElicitationTransport(UnreachableAsErrorTransport(http_transport or httpx2.AsyncHTTPTransport()))
        self._http = httpx2.AsyncClient(transport=boundary, timeout=httpx2.Timeout(timeout))
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> "McpHost":
        self._stack = AsyncExitStack()
        await self._stack.enter_async_context(self._http)
        transport = streamable_http_client(self._url, http_client=self._http, terminate_on_close=False)
        client = Client(
            transport,
            mode=PROTOCOL_VERSION,  # pinned: no discovery handshake, nothing inferred from earlier traffic
            client_info=CLIENT_INFO,
            elicitation_callback=_refuse_elicitation,
            read_timeout_seconds=self._timeout,
            cache=None,
        )
        self._client = await self._stack.enter_async_context(client)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            await stack.aclose()

    async def book(self, request: BookingRequest, *, traceparent: str | None = None) -> BookingOutcome:
        arguments = request.arguments()
        with _translated_errors():
            tool = await self._discover_booking_tool(traceparent)
            _check_contract(tool, arguments)
            policy_version = await self._read_policy_version(traceparent)
            result = await self._call(arguments, None, None, traceparent)
        return _outcome(result, request, policy_version)

    async def resume(
        self, pending: InputRequired, answer: Answer, *, traceparent: str | None = None
    ) -> BookingOutcome:
        """Repeat the original call with the caller's answer and the state exactly as the server gave it."""
        with _translated_errors():
            result = await self._call(
                pending.request.arguments(), {pending.key: answer.to_wire()}, pending.request_state, traceparent
            )
        return _outcome(result, pending.request, pending.policy_version)

    # --- MCP requests --------------------------------------------------------------------------------------------

    async def _discover_booking_tool(self, traceparent: str | None) -> Tool:
        tools: dict[str, Tool] = {}
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            page = await self._require_client().list_tools(cursor=cursor, meta=_meta(traceparent))
            tools.update({tool.name: tool for tool in page.tools})
            cursor = page.next_cursor
            if not cursor:
                break
        if BOOKING_TOOL not in tools:
            raise ContractError(f"the server does not offer the {BOOKING_TOOL} tool (discovered: {sorted(tools)})")
        return tools[BOOKING_TOOL]

    async def _read_policy_version(self, traceparent: str | None) -> str:
        result = await self._require_client().read_resource(POLICY_URI, meta=_meta(traceparent))
        text = next((c.text for c in result.contents if isinstance(getattr(c, "text", None), str)), None)
        first_line = text.splitlines()[0] if text else ""
        match = _POLICY_VERSION_LINE.match(first_line)
        if match is None:
            raise ContractError("the policy resource does not start with a 'versao: <valor>' line")
        return match.group(1)

    async def _call(
        self,
        arguments: dict[str, str],
        input_responses: Mapping[str, Any] | None,
        request_state: str | None,
        traceparent: str | None,
    ) -> CallToolResult | InputRequiredResult:
        # session.call_tool(allow_input_required=True) hands back the raw InputRequiredResult;
        # Client.call_tool would run the elicitation callback by itself and close the loop.
        return await self._require_client().session.call_tool(
            BOOKING_TOOL,
            arguments,
            input_responses=input_responses,
            request_state=request_state,
            meta=_meta(traceparent),
            allow_input_required=True,
        )

    def _require_client(self) -> Client:
        if self._client is None:
            raise McpHostError("McpHost must be used as 'async with McpHost(...)'")
        return self._client


def _meta(traceparent: str | None) -> dict[str, str] | None:
    """Same trace-id as the caller's, a new span-id for this hop; nothing at all if there is no valid trace."""
    child = child_of(traceparent)
    return {"traceparent": child} if child else None


def _check_contract(tool: Tool, arguments: Mapping[str, str]) -> None:
    required = (tool.input_schema or {}).get("required", [])
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ContractError(f"{BOOKING_TOOL} requires arguments the agent does not provide: {', '.join(missing)}")


@contextmanager
def _translated_errors():
    try:
        yield
    except MCPError as error:
        data = error.data
        if error.code == UNREACHABLE_CODE and isinstance(data, dict) and data.get("origin") == UNREACHABLE_ORIGIN:
            raise McpUnavailable(f"MCP server unreachable ({data.get('reason', 'unknown')})") from None
        raise McpProtocolError(error.code, error.message, data) from None
    except ValidationError:
        raise ContractError("the server answered with a message that does not match the MCP schema") from None


def _outcome(
    result: CallToolResult | InputRequiredResult, request: BookingRequest, policy_version: str
) -> BookingOutcome:
    if isinstance(result, InputRequiredResult):
        return _pause(result, request, policy_version)
    if result.is_error:
        return ToolFailed(" ".join(block.text for block in result.content if isinstance(block, TextContent)))
    payload = result.structured_content
    if not isinstance(payload, dict) or "reservado" not in payload:
        raise ContractError("the booking result has no structured content to read")
    if payload["reservado"] is False:
        return Declined(str(payload.get("motivo") or ""), policy_version)
    return Reserved(payload, policy_version)


def _pause(result: InputRequiredResult, request: BookingRequest, policy_version: str) -> InputRequired:
    if len(result.input_requests or {}) != 1 or not result.request_state:
        raise ContractError("expected exactly one input request and a requestState")
    [(key, question)] = result.input_requests.items()
    params = question.params
    if question.method != "elicitation/create" or getattr(params, "mode", None) != "form":
        raise ContractError("expected a form-mode elicitation")
    return InputRequired(
        request=request,
        key=key,
        message=params.message,
        choices=_choices(params.requested_schema),
        input_requests={key: question.model_dump(by_alias=True, mode="json", exclude_none=True)},
        policy_version=policy_version,
        request_state=result.request_state,
    )


def _choices(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """The ids offered by the elicitation, in the order the server listed them (``enum``, or ``const`` for one)."""
    field_schema = (schema.get("properties") or {}).get("sala")
    if not isinstance(field_schema, dict):
        raise ContractError("the elicitation has no 'sala' property")
    if isinstance(field_schema.get("enum"), list):
        values = field_schema["enum"]
    elif "const" in field_schema:
        values = [field_schema["const"]]
    else:
        raise ContractError("the 'sala' property offers neither enum nor const")
    if not values or not all(isinstance(value, str) for value in values):
        raise ContractError("the 'sala' property must offer string ids")
    return tuple(values)
