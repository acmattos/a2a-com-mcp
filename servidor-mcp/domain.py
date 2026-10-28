"""Room booking rules. Pure functions over plain data: no I/O, no MCP, no A2A."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

# Error messages are part of the contract (exact text, Portuguese, no accents).
ERR_WINDOW = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERR_DURATION = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERR_INTERVAL = "Intervalo invalido: fim deve ser posterior a inicio"
ERR_NO_ALTERNATIVES = "Sem alternativas disponiveis no intervalo"
ERR_INVALID_INSTANT = "Data invalida: use ISO-8601 com fuso horario (ex.: 2026-11-03T14:00:00-03:00)"


def err_unknown_room(room_id: str) -> str:
    return f"Sala inexistente: {room_id}"


SAO_PAULO = timezone(timedelta(hours=-3))
OPENING = time(8, 0)
CLOSING = time(20, 0)
MAX_DURATION = timedelta(hours=2)
MAX_ALTERNATIVES = 3


class DomainError(Exception):
    """A business-rule violation; ``str(error)`` is the exact message the tool must return."""


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    capacity: int
    resources: list[str]


@dataclass(frozen=True)
class Reservation:
    id: str
    room: str
    start: datetime
    end: datetime
    owner: str


@dataclass(frozen=True)
class Booking:
    """A request that already passed every validation."""

    room: Room
    start: datetime
    end: datetime


def parse_instant(value: object) -> datetime:
    """Parse an ISO-8601 instant that carries an explicit offset."""
    if not isinstance(value, str):
        raise DomainError(ERR_INVALID_INSTANT)
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        raise DomainError(ERR_INVALID_INSTANT) from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise DomainError(ERR_INVALID_INSTANT)
    return instant


def format_instant(instant: datetime) -> str:
    """Render an instant in São Paulo time, the way the starter data writes it."""
    return instant.astimezone(SAO_PAULO).isoformat()


def validate_booking(rooms: list[Room], room_id: str, start_text: object, end_text: object) -> Booking:
    """Apply the policy in a fixed order: room, interval order, usage window, duration."""
    room = next((r for r in rooms if r.id == room_id), None)
    if room is None:
        raise DomainError(err_unknown_room(room_id))

    start = parse_instant(start_text)
    end = parse_instant(end_text)
    if end <= start:
        raise DomainError(ERR_INTERVAL)

    local_start = start.astimezone(SAO_PAULO)
    local_end = end.astimezone(SAO_PAULO)
    day = local_start.date()
    if local_start.time() < OPENING or local_end.date() != day or local_end.time() > CLOSING:
        raise DomainError(ERR_WINDOW)
    if end - start > MAX_DURATION:
        raise DomainError(ERR_DURATION)
    return Booking(room, start, end)


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """Half-open intervals: adjacent ones do not overlap."""
    return start_a < end_b and start_b < end_a


def find_conflicts(reservations: list[Reservation], room_id: str, start: datetime, end: datetime) -> list[Reservation]:
    hits = [r for r in reservations if r.room == room_id and overlaps(start, end, r.start, r.end)]
    return sorted(hits, key=lambda r: (r.start, r.id))


def find_alternatives(
    rooms: list[Room], reservations: list[Reservation], requested: Room, start: datetime, end: datetime
) -> list[Room]:
    """Free rooms at least as large as the requested one, smallest first, at most three."""
    candidates = [
        r
        for r in rooms
        if r.id != requested.id
        and r.capacity >= requested.capacity
        and not find_conflicts(reservations, r.id, start, end)
    ]
    return sorted(candidates, key=lambda r: (r.capacity, r.id))[:MAX_ALTERNATIVES]
