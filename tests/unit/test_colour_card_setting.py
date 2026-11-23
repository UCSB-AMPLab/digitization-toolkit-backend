"""The unit's colour card setting (NEH-340): whether operators are asked for colour card shots.

- `unit_settings.colour_card_shots` (boolean, NOT NULL, server default
  false): off on a unit that updates to this version, on for a unit whose
  first account is created through POST /auth/register.
- In GET /system/unit as `colourCardShots`, for every role; written by an
  administrator through PUT /system/unit, logged
  `unit_colour_card_shots_changed`.

    python -m pytest tests/unit/test_colour_card_setting.py
"""

import pytest

from tests.unit.test_first_run import PASSWORD, _account, _logs, _register, _unit_row, peer  # noqa: F401


@pytest.mark.unit
def test_a_unit_without_a_row_has_the_setting_off(client, db_session):
    _user, headers = _account(db_session, "reviewer")
    body = client.get("/system/unit", headers=headers).json()
    assert body["colourCardShots"] is False


@pytest.mark.unit
def test_a_row_written_at_start_up_has_the_setting_off(client, db_session):
    from app.core import unit_mode
    unit_mode.initialise()
    assert _unit_row(db_session).colour_card_shots is False


@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer", "testing"])
def test_every_role_reads_the_setting(client, db_session, role):
    from app.core import unit_mode
    unit_mode.set_colour_card_shots(db_session, True, actor="someone")
    _user, headers = _account(db_session, role)
    resp = client.get("/system/unit", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["colourCardShots"] is True


@pytest.mark.unit
def test_the_first_account_turns_the_setting_on(peer, db_session):
    resp = _register(peer("127.0.0.1"))
    assert resp.status_code == 200, resp.text
    assert _unit_row(db_session).colour_card_shots is True
    assert len(_logs(db_session, "unit_colour_card_shots_changed")) == 1


@pytest.mark.unit
def test_a_later_account_leaves_the_setting_as_it_is(peer, db_session):
    from app.core import unit_mode
    _admin, headers = _account(db_session, "admin")
    unit_mode.set_colour_card_shots(db_session, False, actor="admin")
    resp = _register(peer("127.0.0.1"), "second", headers=headers)
    assert resp.status_code == 200, resp.text
    assert _unit_row(db_session).colour_card_shots is False


@pytest.mark.unit
def test_an_administrator_turns_it_on_and_off(client, db_session):
    _admin, headers = _account(db_session, "admin")
    resp = client.put("/system/unit", json={"colourCardShots": True}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["colourCardShots"] is True
    assert _unit_row(db_session).colour_card_shots is True
    resp = client.put("/system/unit", json={"colourCardShots": False}, headers=headers)
    assert resp.json()["colourCardShots"] is False
    assert _unit_row(db_session).colour_card_shots is False
    logs = _logs(db_session, "unit_colour_card_shots_changed")
    assert [log.actor for log in logs] == ["u-admin", "u-admin"]


@pytest.mark.unit
def test_the_setting_alone_changes_nothing_else(client, db_session):
    _admin, headers = _account(db_session, "admin")
    before = client.get("/system/unit", headers=headers).json()
    after = client.put("/system/unit", json={"colourCardShots": True}, headers=headers).json()
    assert {k: v for k, v in after.items() if k != "colourCardShots"} == {
        k: v for k, v in before.items() if k != "colourCardShots"}
    assert _logs(db_session, "unit_mode_changed") == []


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_administrator_writes_it(client, db_session, role):
    _user, headers = _account(db_session, role)
    resp = client.put("/system/unit", json={"colourCardShots": True}, headers=headers)
    assert resp.status_code == 403, resp.text


@pytest.mark.unit
def test_the_setting_takes_a_json_boolean(client, db_session):
    _admin, headers = _account(db_session, "admin")
    resp = client.put("/system/unit", json={"colourCardShots": "yes"}, headers=headers)
    assert resp.status_code == 422, resp.text
