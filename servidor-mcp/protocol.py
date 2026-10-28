"""MCP adapter: exposes ``BookingService`` as tools and a resource. No business rules live here.

No ``from __future__ import annotations`` on purpose: the SDK evaluates the tool annotations, and
``Resolve(escolha_de_sala)`` refers to a local name that only exists when the annotation is evaluated eagerly.
"""

import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, Field, create_model

from domain import ERR_NO_ALTERNATIVES, DomainError, Reservation, format_instant
from service import BookingConflict, BookingService

SERVER_NAME = "central-de-salas"
SERVER_VERSION = "1.0.0"
POLICY_URI = "politica://uso"

ELICITATION_MESSAGE = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
DECLINED_REASON = "recusado"


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


def _chosen_room(choice: ElicitationResult[Any], requested: str) -> str:
    """The room to book: the accepted alternative, or the requested one when nothing was asked."""
    if isinstance(choice, AcceptedElicitation) and choice.data is not None:
        return choice.data.sala
    return requested


def build_server(service: BookingService, security: RequestStateSecurity) -> MCPServer:
    """``security`` is mandatory: there is no default, ephemeral or built-in key to fall back on."""
    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION, request_state_security=security)

    def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[Any] | None:
        """Ask only when the interval is taken and alternatives exist. Invalid input is left to the tool body."""
        try:
            alternatives = service.alternatives_for(sala, inicio, fim)
        except DomainError:
            return None
        if not alternatives:
            return None
        return Elicit(ELICITATION_MESSAGE, _choice_model([room.id for room in alternatives]))

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
        if isinstance(escolha, (DeclinedElicitation, CancelledElicitation)):
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
