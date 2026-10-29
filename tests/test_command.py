import pytest

from command import BookingCommand, InvalidCommand, parse_command
from mcp_host import BookingRequest

START = "2026-11-03T14:00:00-03:00"
END = "2026-11-03T15:00:00-03:00"
VALID = f"reservar sala=sala-garagem inicio={START} fim={END} responsavel=Marty"


def test_a_well_formed_command_becomes_a_booking_request():
    command = parse_command(VALID)
    assert command == BookingCommand(BookingRequest("sala-garagem", START, END, "Marty"))


def test_the_owner_is_the_rest_of_the_line_so_names_may_have_spaces():
    command = parse_command(VALID.replace("Marty", "Marty McFly Jr."))
    assert command.request.owner == "Marty McFly Jr."


def test_surrounding_and_repeated_whitespace_between_fields_is_tolerated():
    spaced = f"  reservar   sala=sala-garagem \t inicio={START}  fim={END}   responsavel=  Marty  \n"
    assert parse_command(spaced).request == BookingRequest("sala-garagem", START, END, "Marty")


def test_the_parser_is_deterministic():
    assert parse_command(VALID) == parse_command(VALID)


@pytest.mark.parametrize("text, reason", [
    ("", "o texto esta vazio"),
    ("   ", "o texto esta vazio"),
    ("ola, quero uma sala", "o comando deve comecar com 'reservar'"),
    ("Reservar sala=a inicio=x fim=y responsavel=z", "o comando deve comecar com 'reservar'"),
    ("reservar", "faltam campos"),
    (f"reservar sala=sala-garagem inicio={START} fim={END}", "faltam campos"),
    (f"reservar inicio={START} fim={END} responsavel=Marty", "faltam campos"),
    (f"reservar sala=sala-garagem fim={END} inicio={START} responsavel=Marty", "os campos devem vir na ordem"),
    (f"reservar sala= inicio={START} fim={END} responsavel=Marty", "o campo sala esta vazio"),
    (f"reservar sala=sala-garagem inicio={START} fim={END} responsavel=", "o campo responsavel esta vazio"),
    (f"reservar sala=sala-garagem inicio=amanha fim={END} responsavel=Marty", "inicio nao e um instante ISO 8601"),
    (f"reservar sala=sala-garagem inicio={START} fim=2026-13-45T99:00:00-03:00 responsavel=Marty",
     "fim nao e um instante ISO 8601"),
    (f"reservar sala=a sala=b inicio={START} fim={END} responsavel=Marty", "os campos devem vir na ordem"),
])
def test_an_invalid_command_is_refused_with_a_stable_explicit_reason(text, reason):
    result = parse_command(text)
    assert isinstance(result, InvalidCommand)
    assert result.reason == reason
    assert result.message == f"Comando invalido: {reason}. Formato: {InvalidCommand.FORMAT}"


def test_the_format_hint_is_the_one_the_agent_card_advertises():
    assert InvalidCommand.FORMAT == "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"


# --- the answer to a pause ---------------------------------------------------------------------------------------

from command import DECLINE, Choice, parse_choice  # noqa: E402


@pytest.mark.parametrize("text, value", [
    ("escolha=sala-mirante", "sala-mirante"),
    ("  escolha=sala-mirante \n", "sala-mirante"),
    ("escolha=recusar", DECLINE),
    ("escolha=SALA-X", "SALA-X"),
])
def test_a_choice_is_read_verbatim(text, value):
    assert parse_choice(text) == Choice(value)


@pytest.mark.parametrize("text", [
    "", "escolha=", "escolha= ", "escolha=a b", "escolha =a", "Escolha=a", "sala-mirante", "reservar sala=a",
    "escolha=a escolha=b",
])
def test_anything_else_is_not_a_choice(text):
    assert parse_choice(text) is None
