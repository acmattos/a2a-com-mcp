"""MCP adapter: exposes ``BookingService`` as tools and a resource. No business rules live here."""

from __future__ import annotations

import json
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel

from domain import DomainError, Reservation, format_instant
from service import BookingConflict, BookingService

SERVER_NAME = "central-de-salas"
SERVER_VERSION = "1.0.0"
POLICY_URI = "politica://uso"

# TEMPORARY (phase 2): until the multi-round-trip flow lands in phase 3, a conflicting
# booking is refused as a plain tool error. It never creates a reservation.
ERR_CONFLICT_PROVISIONAL = "Conflito de horario: a sala ja esta reservada nesse intervalo"


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


def build_server(service: BookingService) -> MCPServer:
    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION)

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
        sala: str, inicio: str, fim: str, responsavel: str
    ) -> Annotated[CallToolResult, ReservaOut]:
        try:
            reservation = service.reserve(sala, inicio, fim, responsavel)
        except DomainError as error:
            return _failure(str(error))
        except BookingConflict:
            return _failure(ERR_CONFLICT_PROVISIONAL)
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
