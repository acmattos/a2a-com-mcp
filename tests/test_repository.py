import json
from datetime import datetime, timedelta, timezone

import pytest

from domain import find_alternatives, find_conflicts, validate_booking
from repository import Repository

SP = timezone(timedelta(hours=-3))


def at(hour: int, day: int = 3) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=SP)


def write_data(directory, reservations, policy="versao: 2026-11-01\n\n- regra\n"):
    (directory / "salas.json").write_text(
        json.dumps([{"id": "sala-a", "nome": "A", "capacidade": 4, "recursos": []}]), encoding="utf-8"
    )
    (directory / "reservas.json").write_text(json.dumps(reservations), encoding="utf-8")
    (directory / "politica-de-uso.md").write_text(policy, encoding="utf-8")
    return directory


def test_loads_the_five_rooms_and_two_initial_reservations_from_the_starter_data():
    repo = Repository()
    assert [r.id for r in repo.rooms()] == [
        "sala-aquario", "sala-porao", "sala-garagem", "sala-fusca", "sala-mirante",
    ]
    assert repo.room("sala-fusca").capacity == 12
    assert [(r.id, r.room, r.owner) for r in repo.reservations()] == [
        ("res-0001", "sala-garagem", "Marty"),
        ("res-0002", "sala-fusca", "Jennifer"),
    ]
    assert repo.reservations()[0].start == at(14) and repo.reservations()[0].end == at(15)


def test_unknown_room_lookup_returns_none():
    assert Repository().room("sala-delorean") is None


def test_policy_text_is_the_original_markdown_and_version_is_read_from_it():
    repo = Repository()
    assert repo.policy_text.startswith("versao: 2026-11-01\n")
    assert "Duracao maxima de 2 horas" in repo.policy_text
    assert repo.policy_version == "2026-11-01"


def test_policy_version_follows_the_file_not_a_constant(tmp_path):
    repo = Repository(write_data(tmp_path, [], policy="versao: 2030-01-01\n\ntexto\n"))
    assert repo.policy_version == "2030-01-01"


def test_policy_without_version_line_fails_loudly(tmp_path):
    with pytest.raises(ValueError):
        Repository(write_data(tmp_path, [], policy="sem versao\n"))


def test_data_is_found_regardless_of_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert len(Repository().rooms()) == 5


def test_first_new_reservation_gets_res_0003_and_ids_never_collide():
    repo = Repository()
    first = repo.add("sala-aquario", at(9), at(10), "Doc")
    second = repo.add("sala-aquario", at(10), at(11), "Doc")
    assert (first.id, second.id) == ("res-0003", "res-0004")
    assert len({r.id for r in repo.reservations()}) == 4


def test_ids_continue_after_the_highest_existing_id(tmp_path):
    initial = [{"id": "res-0010", "sala": "sala-a", "inicio": "2026-11-03T09:00:00-03:00",
                "fim": "2026-11-03T10:00:00-03:00", "responsavel": "X"}]
    repo = Repository(write_data(tmp_path, initial))
    assert repo.add("sala-a", at(11), at(12), "Y").id == "res-0011"


def test_new_reservation_is_visible_to_later_queries_in_the_same_process():
    repo = Repository()
    assert find_conflicts(repo.reservations(), "sala-aquario", at(9), at(10)) == []
    created = repo.add("sala-aquario", at(9), at(10), "Doc")
    assert [r.id for r in find_conflicts(repo.reservations(), "sala-aquario", at(9), at(10))] == [created.id]


def test_new_reservation_removes_the_room_from_alternatives():
    repo = Repository()
    garagem = repo.room("sala-garagem")
    before = [r.id for r in find_alternatives(repo.rooms(), repo.reservations(), garagem, at(14), at(15))]
    repo.add("sala-fusca", at(14), at(15), "Doc")
    after = [r.id for r in find_alternatives(repo.rooms(), repo.reservations(), garagem, at(14), at(15))]
    assert (before, after) == (["sala-fusca", "sala-mirante"], ["sala-mirante"])


def test_returned_reservation_list_is_a_copy():
    repo = Repository()
    repo.reservations().clear()
    assert len(repo.reservations()) == 2


def test_a_new_repository_starts_from_the_initial_data_again():
    repo = Repository()
    repo.add("sala-aquario", at(9), at(10), "Doc")
    assert len(Repository().reservations()) == 2


def test_validation_and_repository_work_together_end_to_end():
    repo = Repository()
    booking = validate_booking(repo.rooms(), "sala-garagem", "2026-11-03T14:00:00-03:00", "2026-11-03T15:00:00-03:00")
    conflicts = find_conflicts(repo.reservations(), booking.room.id, booking.start, booking.end)
    assert [c.id for c in conflicts] == ["res-0001"]
