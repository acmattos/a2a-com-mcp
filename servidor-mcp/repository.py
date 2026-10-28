"""Rooms, reservations and policy loaded from ``dados/``; new reservations live in memory only."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from domain import Reservation, Room, parse_instant

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "dados"
_POLICY_VERSION = re.compile(r"^versao:\s*(\S+)\s*$")
_ID_NUMBER = re.compile(r"^res-(\d+)$")


class Repository:
    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR) -> None:
        data = Path(data_dir)
        self._rooms = [
            Room(r["id"], r["nome"], r["capacidade"], list(r["recursos"]))
            for r in _read_json(data / "salas.json")
        ]
        self._reservations = [
            Reservation(r["id"], r["sala"], parse_instant(r["inicio"]), parse_instant(r["fim"]), r["responsavel"])
            for r in _read_json(data / "reservas.json")
        ]
        self.policy_text = (data / "politica-de-uso.md").read_text(encoding="utf-8")
        self.policy_version = _read_policy_version(self.policy_text)
        self._next_number = max((_id_number(r.id) for r in self._reservations), default=0) + 1

    def rooms(self) -> list[Room]:
        return list(self._rooms)

    def room(self, room_id: str) -> Room | None:
        return next((r for r in self._rooms if r.id == room_id), None)

    def reservations(self) -> list[Reservation]:
        return list(self._reservations)

    def add(self, room_id: str, start: datetime, end: datetime, owner: str) -> Reservation:
        reservation = Reservation(f"res-{self._next_number:04d}", room_id, start, end, owner)
        self._next_number += 1
        self._reservations.append(reservation)
        return reservation


def _read_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_policy_version(text: str) -> str:
    first_line = text.splitlines()[0] if text else ""
    match = _POLICY_VERSION.match(first_line)
    if match is None:
        raise ValueError("politica-de-uso.md must start with a 'versao: <valor>' line")
    return match.group(1)


def _id_number(reservation_id: str) -> int:
    match = _ID_NUMBER.match(reservation_id)
    return int(match.group(1)) if match else 0
