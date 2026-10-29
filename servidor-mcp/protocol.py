"""MCP adapter: exposes ``BookingService`` as tools and a resource. No business rules live here.

No ``from __future__ import annotations`` on purpose: the SDK evaluates the tool annotations, and
``Resolve(escolha_de_sala)`` refers to a local name that only exists when the annotation is evaluated eagerly.
"""

import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import _decode_state, _render_request, _request_digest  # see _standing_answer
from mcp_types import CallToolResult, ElicitResult, TextContent
from pydantic import BaseModel, Field, ValidationError, create_model

from domain import ERR_NO_ALTERNATIVES, DomainError, Reservation, Room, format_instant
from service import BookingConflict, BookingService

SERVER_NAME = "central-de-salas"
SERVER_VERSION = "1.0.0"
POLICY_URI = "politica://uso"

ELICITATION_MESSAGE = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
DECLINED_REASON = "recusado"
UNOFFERED_CHOICE = "Escolha invalida: a sala nao esta entre as alternativas oferecidas"


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def _success(output: BaseModel) -> CallToolResult:
    """Structured content and the text block carry the very same JSON object."""
    payload = output.model_dump(mode="json")
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)


def _failure(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


def _conflict_out(reservation: Reservation) -> ConflitoOut:
    return ConflitoOut(
        id=reservation.id,
        inicio=format_instant(reservation.start),
        fim=format_instant(reservation.end),
        responsavel=reservation.owner,
    )


def _choice_model(room_ids: list[str]) -> type[BaseModel]:
    """Flat form with one required string property limited to the offered rooms."""
    return create_model(
        "EscolhaDeSala", sala=(Literal[tuple(room_ids)], Field(description="Sala alternativa escolhida"))
    )


class Refusal:
    """The client declined or cancelled the question it was asked."""


def _standing_answer(ctx: Context, offers: list[list[Room]]) -> BaseModel | Refusal | None:
    """The client's answer to the question it was asked, when it can still be honoured; ``None`` to leave it to the SDK.

    The SDK seals, with the token, the wire key and a digest of the exact question it asked, and it drops every
    answer whose question changed (rooms booked meanwhile, memory lost in a restart). The token reaches us already
    authenticated, so that digest can be trusted: the one possible offer whose rendering hashes to it is the list the
    client actually saw. Honoured here: a refusal (nothing is booked, whatever the memory looks like now), and the
    choice of a room *from that list*. A room that was not offered is never an answer, free today or not.
    """
    answers = ctx.input_responses
    if not ctx.request_state or not answers:
        return None
    asked = _decode_state(ctx.request_state).asked
    if len(asked) != 1:
        return None
    [(key, digest)] = asked.items()
    answer = answers.get(key)
    if not isinstance(answer, ElicitResult):
        return None
    if answer.action in ("decline", "cancel"):
        return Refusal()
    for offer in offers:
        ids = [room.id for room in offer]
        if _request_digest(_render_request(Elicit(ELICITATION_MESSAGE, _choice_model(ids)))) == digest:
            try:
                return _choice_model(ids).model_validate(answer.content)
            except ValidationError:
                return None
    return None


def _is_refusal(choice: ElicitationResult[Any]) -> bool:
    if isinstance(choice, (DeclinedElicitation, CancelledElicitation)):
        return True
    return isinstance(choice, AcceptedElicitation) and isinstance(choice.data, Refusal)


def _chosen_room(choice: ElicitationResult[Any], requested: str) -> str:
    """The room to book: the accepted alternative, or the requested one when nothing was asked."""
    if isinstance(choice, AcceptedElicitation) and choice.data is not None:
        return choice.data.sala
    return requested


def build_server(service: BookingService, security: RequestStateSecurity) -> MCPServer:
    """``security`` is mandatory: there is no default, ephemeral or built-in key to fall back on."""
    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION, request_state_security=security)

    def escolha_de_sala(sala: str, inicio: str, fim: str, ctx: Context) -> Elicit[Any] | BaseModel | Refusal | None:
        """Ask only when the interval is taken and alternatives exist. Invalid input is left to the tool body."""
        try:
            alternatives = service.alternatives_for(sala, inicio, fim)
            offers = service.possible_offers(sala, inicio, fim)
        except DomainError:
            return None
        live_ids = [room.id for room in alternatives]
        standing = _standing_answer(ctx, offers)
        if isinstance(standing, Refusal):
            return standing
        if standing is not None and (standing.sala in live_ids or not live_ids):
            return standing  # offered earlier; free now, or nothing left to ask (the booking is checked by the service)
        if not alternatives:
            if ctx.request_state and ctx.input_responses:
                raise ToolError(UNOFFERED_CHOICE)  # an answer that cannot be honoured, and no new question to ask
            return None
        return Elicit(ELICITATION_MESSAGE, _choice_model(live_ids))

    @server.tool(name="listar_salas", description="Lista todas as salas com capacidade e recursos.")
    def listar_salas() -> Annotated[CallToolResult, ListaDeSalas]:
        rooms = [
            SalaOut(id=r.id, nome=r.name, capacidade=r.capacity, recursos=r.resources)
            for r in service.list_rooms()
        ]
        return _success(ListaDeSalas(salas=rooms))

    @server.tool(
        name="consultar_disponibilidade",
        description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.",
    )
    def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Annotated[CallToolResult, Disponibilidade]:
        try:
            availability = service.check_availability(sala, inicio, fim)
        except DomainError as error:
            return _failure(str(error))
        return _success(
            Disponibilidade(
                sala=availability.room.id,
                livre=availability.free,
                conflitos=[_conflict_out(c) for c in availability.conflicts],
            )
        )

    @server.tool(
        name="reservar_sala",
        description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.",
    )
    def reservar_sala(
        sala: str,
        inicio: str,
        fim: str,
        responsavel: str,
        escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
    ) -> Annotated[CallToolResult, ReservaOut]:
        if _is_refusal(escolha):
            return _success(ReservaOut(reservado=False, motivo=DECLINED_REASON))
        try:
            reservation = service.reserve(_chosen_room(escolha, sala), inicio, fim, responsavel)
        except DomainError as error:
            return _failure(str(error))
        except BookingConflict:
            # Nothing to offer, or the chosen room was taken meanwhile: either way, no room fits.
            return _failure(ERR_NO_ALTERNATIVES)
        return _success(
            ReservaOut(
                reserva=reservation.id,
                reservado=True,
                sala=reservation.room,
                inicio=format_instant(reservation.start),
                fim=format_instant(reservation.end),
                responsavel=reservation.owner,
                politica=service.policy_version,
            )
        )

    @server.resource(
        POLICY_URI,
        name="politica-de-uso",
        title="Politica de uso das salas",
        description="Regras de uso das salas de reuniao (Markdown).",
        mime_type="text/markdown",
    )
    def politica_de_uso() -> str:
        return service.policy_text

    return server
