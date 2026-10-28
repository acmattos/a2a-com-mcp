import random
from datetime import datetime, timedelta, timezone

import pytest

from domain import (
    ERR_DURATION,
    ERR_INTERVAL,
    ERR_NO_ALTERNATIVES,
    ERR_WINDOW,
    DomainError,
    Reservation,
    Room,
    find_alternatives,
    find_conflicts,
    format_instant,
    validate_booking,
)

SP = timezone(timedelta(hours=-3))

ROOMS = [
    Room("sala-aquario", "Aquario", 4, ["tv"]),
    Room("sala-porao", "Porao", 6, ["quadro"]),
    Room("sala-garagem", "Garagem", 12, ["tv", "quadro"]),
    Room("sala-fusca", "Fusca", 12, ["tv"]),
    Room("sala-mirante", "Mirante", 20, ["tv", "quadro", "camera"]),
]


def at(hour: int, minute: int = 0, day: int = 3) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=SP)


def iso(hour: int, minute: int = 0, day: int = 3) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:{minute:02d}:00-03:00"


def booking(room: str, start: datetime, end: datetime, rid: str = "res-x") -> Reservation:
    return Reservation(rid, room, start, end, "Fulano")


INITIAL = [
    booking("sala-garagem", at(14), at(15), "res-0001"),
    booking("sala-fusca", at(16), at(17), "res-0002"),
]


def message_of(start: str, end: str, room: str = "sala-aquario") -> str:
    with pytest.raises(DomainError) as exc:
        validate_booking(ROOMS, room, start, end)
    return str(exc.value)


# --- validate_booking: rooms and error messages -------------------------------------------------


def test_valid_booking_returns_room_and_parsed_interval():
    result = validate_booking(ROOMS, "sala-porao", iso(9), iso(10))
    assert result.room.id == "sala-porao"
    assert result.start == at(9) and result.end == at(10)


def test_unknown_room_message_includes_given_id():
    assert message_of(iso(9), iso(10), room="sala-delorean") == "Sala inexistente: sala-delorean"


def test_unknown_room_wins_over_other_errors():
    assert message_of(iso(7), iso(23), room="sala-delorean") == "Sala inexistente: sala-delorean"


def test_error_messages_are_the_exact_literals():
    assert ERR_WINDOW == "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
    assert ERR_DURATION == "Duracao acima do limite: a politica permite no maximo 2 horas"
    assert ERR_INTERVAL == "Intervalo invalido: fim deve ser posterior a inicio"
    assert ERR_NO_ALTERNATIVES == "Sem alternativas disponiveis no intervalo"


# --- interval order -------------------------------------------------------------------------------


def test_inverted_interval():
    assert message_of(iso(10), iso(9)) == ERR_INTERVAL


def test_empty_interval():
    assert message_of(iso(10), iso(10)) == ERR_INTERVAL


def test_inverted_interval_wins_over_window():
    assert message_of(iso(21), iso(7)) == ERR_INTERVAL


# --- usage window: 08:00..20:00 inclusive at both ends --------------------------------------------


def test_start_exactly_at_opening_is_allowed():
    assert validate_booking(ROOMS, "sala-aquario", iso(8), iso(9)).start == at(8)


def test_end_exactly_at_closing_is_allowed():
    assert validate_booking(ROOMS, "sala-aquario", iso(19), iso(20)).end == at(20)


def test_start_one_minute_before_opening_is_rejected():
    assert message_of(iso(7, 59), iso(9)) == ERR_WINDOW


def test_end_one_minute_after_closing_is_rejected():
    assert message_of(iso(19), iso(20, 1)) == ERR_WINDOW


def test_start_at_closing_is_rejected():
    assert message_of(iso(20), iso(21)) == ERR_WINDOW


def test_interval_entirely_before_opening_is_rejected():
    assert message_of(iso(6), iso(7)) == ERR_WINDOW


def test_interval_crossing_midnight_is_rejected_as_window():
    assert message_of(iso(19), iso(9, day=4)) == ERR_WINDOW


def test_window_is_checked_in_sao_paulo_time_for_other_offsets():
    # 12:00Z is 09:00-03:00 (inside); 23:30Z is 20:30-03:00 (outside)
    ok = validate_booking(ROOMS, "sala-aquario", "2026-11-03T12:00:00Z", "2026-11-03T13:00:00Z")
    assert ok.start == at(9)
    assert message_of("2026-11-03T22:30:00Z", "2026-11-03T23:30:00Z") == ERR_WINDOW


def test_window_wins_over_duration():
    assert message_of(iso(7), iso(10)) == ERR_WINDOW


# --- maximum duration: 2h ---------------------------------------------------------------------------


def test_exactly_two_hours_is_allowed():
    assert validate_booking(ROOMS, "sala-aquario", iso(9), iso(11)).end == at(11)


def test_two_hours_and_one_minute_is_rejected():
    assert message_of(iso(9), iso(11, 1)) == ERR_DURATION


def test_three_hours_is_rejected():
    assert message_of(iso(9), iso(12)) == ERR_DURATION


# --- input parsing ------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "amanha", "2026-11-03", "2026-13-03T09:00:00-03:00", "2026-11-03T09:00:00"])
def test_unparseable_or_naive_instants_raise_domain_error_not_crash(bad):
    with pytest.raises(DomainError):
        validate_booking(ROOMS, "sala-aquario", bad, iso(10))
    with pytest.raises(DomainError):
        validate_booking(ROOMS, "sala-aquario", iso(9), bad)


def test_non_string_instant_raises_domain_error():
    with pytest.raises(DomainError):
        validate_booking(ROOMS, "sala-aquario", None, iso(10))


def test_past_dates_are_allowed():
    result = validate_booking(ROOMS, "sala-aquario", "2020-01-02T09:00:00-03:00", "2020-01-02T10:00:00-03:00")
    assert result.start.year == 2020


def test_format_instant_normalizes_to_sao_paulo_offset():
    assert format_instant(datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)) == "2026-11-03T09:00:00-03:00"
    assert format_instant(at(14)) == "2026-11-03T14:00:00-03:00"


# --- conflicts ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end",
    [
        (at(14), at(15)),  # identical
        (at(13, 30), at(14, 30)),  # overlaps the beginning
        (at(14, 30), at(15, 30)),  # overlaps the end
        (at(14, 15), at(14, 45)),  # contained
        (at(13), at(16)),  # contains
    ],
)
def test_overlapping_intervals_conflict(start, end):
    assert [r.id for r in find_conflicts(INITIAL, "sala-garagem", start, end)] == ["res-0001"]


@pytest.mark.parametrize("start,end", [(at(13), at(14)), (at(15), at(16)), (at(8), at(9))])
def test_adjacent_or_distant_intervals_do_not_conflict(start, end):
    assert find_conflicts(INITIAL, "sala-garagem", start, end) == []


def test_other_rooms_reservations_are_ignored():
    assert find_conflicts(INITIAL, "sala-aquario", at(14), at(15)) == []


def test_conflicts_are_sorted_by_start():
    extra = [booking("sala-porao", at(11), at(12), "b"), booking("sala-porao", at(9), at(10), "a")]
    assert [r.id for r in find_conflicts(extra, "sala-porao", at(9), at(12))] == ["a", "b"]


# --- alternatives ------------------------------------------------------------------------------------


def alternatives(room_id: str, start: datetime, end: datetime, reservations=INITIAL, rooms=ROOMS) -> list[str]:
    room = next(r for r in rooms if r.id == room_id)
    return [r.id for r in find_alternatives(rooms, reservations, room, start, end)]


def test_initial_scenario_offers_fusca_then_mirante():
    assert alternatives("sala-garagem", at(14), at(15)) == ["sala-fusca", "sala-mirante"]


def test_equal_capacity_is_included_and_ties_break_by_id():
    # fusca and garagem both seat 12: requesting fusca must offer garagem before mirante
    assert alternatives("sala-fusca", at(16), at(17)) == ["sala-garagem", "sala-mirante"]


def test_smaller_rooms_are_never_offered():
    assert alternatives("sala-porao", at(9), at(10)) == ["sala-fusca", "sala-garagem", "sala-mirante"]


def test_busy_rooms_are_not_offered():
    busy = INITIAL + [booking("sala-fusca", at(14), at(15), "res-9")]
    assert alternatives("sala-garagem", at(14), at(15), busy) == ["sala-mirante"]


def test_adjacent_reservation_does_not_make_a_room_busy():
    adjacent = [booking("sala-fusca", at(15), at(16), "res-9")]
    assert alternatives("sala-garagem", at(14), at(15), adjacent) == ["sala-fusca", "sala-mirante"]


def test_requested_room_is_never_its_own_alternative():
    assert "sala-garagem" not in alternatives("sala-garagem", at(14), at(15), [])


def test_at_most_three_alternatives():
    # aquario has four larger candidates: porao, fusca, garagem, mirante
    assert alternatives("sala-aquario", at(9), at(10)) == ["sala-porao", "sala-fusca", "sala-garagem"]


def test_no_alternatives_for_the_largest_room():
    busy = [booking("sala-mirante", at(11), at(12), "res-9")]
    assert alternatives("sala-mirante", at(11), at(12), busy) == []


def test_no_alternatives_when_every_larger_room_is_busy():
    busy = INITIAL + [booking("sala-mirante", at(14), at(15), "res-9"), booking("sala-fusca", at(14), at(15), "res-8")]
    assert alternatives("sala-garagem", at(14), at(15), busy) == []


def test_alternative_order_does_not_depend_on_input_order():
    for seed in range(10):
        shuffled = ROOMS[:]
        random.Random(seed).shuffle(shuffled)
        assert alternatives("sala-aquario", at(9), at(10), [], shuffled) == ["sala-porao", "sala-fusca", "sala-garagem"]


def test_alternatives_are_deterministic_across_calls():
    first = alternatives("sala-garagem", at(14), at(15))
    assert all(alternatives("sala-garagem", at(14), at(15)) == first for _ in range(5))
