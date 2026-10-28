import pytest

from domain import ERR_INTERVAL, ERR_WINDOW, DomainError
from repository import Repository
from service import BookingConflict, BookingService


def iso(hour: int, minute: int = 0) -> str:
    return f"2026-11-03T{hour:02d}:{minute:02d}:00-03:00"


@pytest.fixture
def repo() -> Repository:
    return Repository()


@pytest.fixture
def service(repo) -> BookingService:
    return BookingService(repo)


def test_list_rooms_returns_every_room_of_the_repository(service):
    assert [r.id for r in service.list_rooms()] == [
        "sala-aquario", "sala-porao", "sala-garagem", "sala-fusca", "sala-mirante",
    ]


def test_policy_version_comes_from_the_policy_file(service):
    assert service.policy_version == "2026-11-01"


def test_free_slot_is_reported_free_without_conflicts(service):
    availability = service.check_availability("sala-aquario", iso(9), iso(10))
    assert availability.room.id == "sala-aquario"
    assert availability.free is True
    assert availability.conflicts == []


def test_busy_slot_lists_the_conflicting_reservations(service):
    availability = service.check_availability("sala-garagem", iso(14, 30), iso(15, 30))
    assert availability.free is False
    assert [c.id for c in availability.conflicts] == ["res-0001"]


def test_availability_applies_the_same_validations_as_booking(service):
    with pytest.raises(DomainError) as window:
        service.check_availability("sala-aquario", iso(7), iso(8))
    with pytest.raises(DomainError) as interval:
        service.check_availability("sala-aquario", iso(10), iso(9))
    assert str(window.value) == ERR_WINDOW
    assert str(interval.value) == ERR_INTERVAL


def test_reserve_registers_the_booking_and_it_becomes_visible(service):
    created = service.reserve("sala-aquario", iso(9), iso(10), "Doc")
    assert (created.id, created.room, created.owner) == ("res-0003", "sala-aquario", "Doc")
    assert service.check_availability("sala-aquario", iso(9), iso(10)).free is False


def test_reserve_rejects_invalid_requests_without_registering_anything(service, repo):
    with pytest.raises(DomainError):
        service.reserve("sala-delorean", iso(9), iso(10), "Doc")
    with pytest.raises(DomainError):
        service.reserve("sala-aquario", iso(7), iso(8), "Doc")
    assert service.check_availability("sala-aquario", iso(8), iso(9)).free is True
    assert len(repo.reservations()) == 2


def test_reserve_on_conflict_raises_and_registers_nothing(service, repo):
    with pytest.raises(BookingConflict) as exc:
        service.reserve("sala-garagem", iso(14), iso(15), "Biff")
    assert [c.id for c in exc.value.conflicts] == ["res-0001"]
    assert len(repo.reservations()) == 2


def test_adjacent_booking_is_not_a_conflict(service):
    assert service.reserve("sala-garagem", iso(15), iso(16), "Doc").room == "sala-garagem"


# --- alternatives (phase 3) -------------------------------------------------------------------------------


def alternative_ids(service, room, start, end) -> list[str]:
    return [r.id for r in service.alternatives_for(room, start, end)]


def test_alternatives_for_a_conflict_follow_the_capacity_then_id_order(service):
    assert alternative_ids(service, "sala-garagem", iso(14), iso(15)) == ["sala-fusca", "sala-mirante"]


def test_no_alternatives_are_offered_when_the_requested_room_is_free(service):
    assert alternative_ids(service, "sala-aquario", iso(9), iso(10)) == []


def test_alternatives_never_include_rooms_that_are_busy_at_that_time(service):
    service.reserve("sala-fusca", iso(14), iso(15), "Doc")
    assert alternative_ids(service, "sala-garagem", iso(14), iso(15)) == ["sala-mirante"]


def test_alternatives_are_empty_when_every_larger_room_is_busy(service):
    service.reserve("sala-fusca", iso(14), iso(15), "Doc")
    service.reserve("sala-mirante", iso(14), iso(15), "Doc")
    assert alternative_ids(service, "sala-garagem", iso(14), iso(15)) == []


def test_the_largest_room_never_has_alternatives(service):
    service.reserve("sala-mirante", iso(11), iso(12), "Doc")
    assert alternative_ids(service, "sala-mirante", iso(11), iso(12)) == []


def test_alternatives_are_capped_at_three(service):
    service.reserve("sala-aquario", iso(9), iso(10), "Doc")
    assert alternative_ids(service, "sala-aquario", iso(9), iso(10)) == ["sala-porao", "sala-fusca", "sala-garagem"]


def test_alternatives_apply_the_same_validations_as_booking(service):
    with pytest.raises(DomainError):
        service.alternatives_for("sala-delorean", iso(9), iso(10))
    with pytest.raises(DomainError):
        service.alternatives_for("sala-aquario", iso(7), iso(8))
