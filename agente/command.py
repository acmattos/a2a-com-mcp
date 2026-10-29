"""The only grammar the agent understands:

    reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>

Deterministic and strict: fixed field order, ``responsavel`` takes the rest of the line (names may have spaces).
The parser checks shape only. Whether the room exists, the window, the duration and the conflicts belong to the
MCP server, so this module knows no room, hour or policy value.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from mcp_host import BookingRequest

VERB = "reservar"
FIELDS = ("sala", "inicio", "fim", "responsavel")
_TIMESTAMP_FIELDS = ("inicio", "fim")


@dataclass(frozen=True)
class BookingCommand:
    request: BookingRequest


@dataclass(frozen=True)
class InvalidCommand:
    """A refusal that always reads the same for the same input: reason first, then the expected format."""

    reason: str
    FORMAT: ClassVar[str] = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"

    @property
    def message(self) -> str:
        return f"Comando invalido: {self.reason}. Formato: {self.FORMAT}"


def parse_command(text: str) -> BookingCommand | InvalidCommand:
    stripped = text.strip()
    if not stripped:
        return InvalidCommand("o texto esta vazio")
    verb, _, rest = stripped.partition(" ")
    if verb != VERB:
        return InvalidCommand(f"o comando deve comecar com '{VERB}'")
    tokens = rest.split(None, len(FIELDS) - 1)
    if len(tokens) < len(FIELDS):
        return InvalidCommand("faltam campos")
    values: dict[str, str] = {}
    for expected, token in zip(FIELDS, tokens):
        key, separator, value = token.partition("=")
        if not separator or key != expected:
            return InvalidCommand("os campos devem vir na ordem")
        value = value.strip()
        if not value:
            return InvalidCommand(f"o campo {expected} esta vazio")
        if expected in _TIMESTAMP_FIELDS and not _is_iso_instant(value):
            return InvalidCommand(f"{expected} nao e um instante ISO 8601")
        values[expected] = value
    return BookingCommand(BookingRequest(values["sala"], values["inicio"], values["fim"], values["responsavel"]))


def _is_iso_instant(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


CHOICE_PREFIX = "escolha="
DECLINE = "recusar"


@dataclass(frozen=True)
class Choice:
    """The answer to a pause: a room id, or ``recusar``. Whether it is one of the offered ids is the bridge's call."""

    value: str


def parse_choice(text: str) -> Choice | None:
    stripped = text.strip()
    if not stripped.startswith(CHOICE_PREFIX):
        return None
    value = stripped[len(CHOICE_PREFIX):]
    if not value or any(ch.isspace() for ch in value):
        return None
    return Choice(value)
