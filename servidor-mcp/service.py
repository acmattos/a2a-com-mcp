"""Booking use cases. Orchestrates domain rules and repository; knows nothing about MCP."""

from __future__ import annotations

from dataclasses import dataclass

from domain import Reservation, Room, find_conflicts, validate_booking
from repository import Repository


@dataclass(frozen=True)
class Availability:
    room: Room
    free: bool
    conflicts: list[Reservation]


class BookingConflict(Exception):
    """The requested interval overlaps existing reservations."""

    def __init__(self, conflicts: list[Reservation]) -> None:
        super().__init__("booking conflict")
        self.conflicts = conflicts


class BookingService:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    @property
    def policy_text(self) -> str:
        return self._repository.policy_text

    @property
    def policy_version(self) -> str:
        return self._repository.policy_version

    def list_rooms(self) -> list[Room]:
        return self._repository.rooms()

    def check_availability(self, room_id: str, start_text: object, end_text: object) -> Availability:
        booking = validate_booking(self._repository.rooms(), room_id, start_text, end_text)
        conflicts = find_conflicts(self._repository.reservations(), booking.room.id, booking.start, booking.end)
        return Availability(booking.room, not conflicts, conflicts)

    def reserve(self, room_id: str, start_text: object, end_text: object, owner: str) -> Reservation:
        """Validate, check for conflicts and only then register the reservation."""
        booking = validate_booking(self._repository.rooms(), room_id, start_text, end_text)
        conflicts = find_conflicts(self._repository.reservations(), booking.room.id, booking.start, booking.end)
        if conflicts:
            raise BookingConflict(conflicts)
        return self._repository.add(booking.room.id, booking.start, booking.end, owner)
