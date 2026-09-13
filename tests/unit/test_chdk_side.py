"""Assigning a body's page parity from the appliance.

The parity lives on the camera's own card, so the only way to change it
without pulling the card is to write A/OWN.TXT over USB. That is what this
route is for: an operator who has just plugged in a body with no parity, or
two bodies that both came up EVEN, can fix it from the dashboard's own host
instead of finding a card reader.

It rewrites a body's identity, so it is an admin's to do. It also mints an id
for a body that has none, which is what lifts that body out of provisional
and lets it capture.
"""

import pytest

import capture.service as capture_service

from .chdk_fakes import Body, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"


def _user(username, role):
    from app.models.user import User

    return User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        role=role,
        is_active=True,
    )


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install(monkeypatch, backend):
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    return backend


# --- the backend method ----------------------------------------------------

@pytest.mark.unit
def test_assigning_a_parity_writes_the_card_and_keeps_the_id(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    rows = backend.assign_side(0, "odd")

    remote, payload = body.uploads[-1]
    assert remote == "A/OWN.TXT"
    assert payload == b"ODD\nid=aaaaaaaaaaaa\n", "the body's id was not preserved"
    assert rows[0]["side"] == "odd"
    assert rows[0]["index"] == 1, "the row must come back at its new index"


@pytest.mark.unit
def test_a_body_with_no_id_is_given_one(monkeypatch):
    """This is what lifts a body out of provisional and lets it capture."""
    body = Body(serial=None, card=b"EVEN\n")
    backend = make_backend(monkeypatch, make_pychdk(body))
    assert backend.list_devices()[0]["provisional"] is True

    rows = backend.assign_side(0, "even")

    _remote, payload = body.uploads[-1]
    text = payload.decode()
    assert text.startswith("EVEN\n")
    assert "id=" in text
    assert rows[0]["provisional"] is False
    assert rows[0]["error"] is None


@pytest.mark.unit
def test_the_parity_the_other_body_already_shoots_is_refused(monkeypatch):
    from capture.backends.chdk_backend import SideConflictError

    even = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    odd = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD)
    backend = make_backend(monkeypatch, make_pychdk(even, odd))
    backend.list_devices()

    with pytest.raises(SideConflictError) as exc:
        backend.assign_side(0, "odd")

    assert "BBB222" in str(exc.value) or "usb:001,007" in str(exc.value)
    assert even.uploads == [], "the card was written despite the conflict"


@pytest.mark.unit
def test_assigning_the_parity_a_body_already_has_is_not_a_conflict(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    rows = backend.assign_side(0, "even")

    assert rows[0]["side"] == "even"
    assert len(body.uploads) == 1


@pytest.mark.unit
def test_a_parity_that_is_not_a_parity_is_refused(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(ValueError):
        backend.assign_side(0, "left")


@pytest.mark.unit
def test_assigning_to_an_index_with_no_body_says_so(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.assign_side(1, "odd")

    assert "not connected" in str(exc.value)


@pytest.mark.unit
def test_two_bodies_on_one_parity_can_be_told_apart_again(monkeypatch):
    """The case the route exists for: both cards say EVEN."""
    first = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    second = Body(bus=1, address=7, serial="BBB222", card=b"EVEN\nid=cccccccccccc\n")
    backend = make_backend(monkeypatch, make_pychdk(first, second))
    assert all(row["error"] for row in backend.list_devices())

    rows = backend.assign_side(1, "odd")

    assert {row["index"]: row["side"] for row in rows} == {0: "even", 1: "odd"}
    assert all(row["error"] is None for row in rows)


# --- the route -------------------------------------------------------------

@pytest.mark.unit
def test_an_admin_can_assign_a_parity(client, monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = _install(monkeypatch, make_backend(monkeypatch, make_pychdk(body)))
    backend.list_devices()
    api = _client_as(client, "boss", "admin")

    resp = api.post("/cameras/side/0", json={"side": "odd"})

    assert resp.status_code == 200, resp.text
    assert [row["side"] for row in resp.json()] == ["odd"]
    assert body.uploads[-1][1] == b"ODD\nid=aaaaaaaaaaaa\n"


@pytest.mark.unit
def test_an_operator_may_not_assign_a_parity(client, monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = _install(monkeypatch, make_backend(monkeypatch, make_pychdk(body)))
    backend.list_devices()
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/side/0", json={"side": "odd"})

    assert resp.status_code == 403, resp.text
    assert body.uploads == []


@pytest.mark.unit
def test_a_parity_the_other_body_holds_is_a_conflict(client, monkeypatch):
    even = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    odd = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD)
    backend = _install(
        monkeypatch, make_backend(monkeypatch, make_pychdk(even, odd))
    )
    backend.list_devices()
    api = _client_as(client, "boss", "admin")

    resp = api.post("/cameras/side/0", json={"side": "odd"})

    assert resp.status_code == 409, resp.text
    assert "odd" in resp.json()["detail"]


@pytest.mark.unit
def test_an_index_with_no_body_is_a_404(client, monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = _install(monkeypatch, make_backend(monkeypatch, make_pychdk(body)))
    backend.list_devices()
    api = _client_as(client, "boss", "admin")

    resp = api.post("/cameras/side/1", json={"side": "odd"})

    assert resp.status_code == 404, resp.text


@pytest.mark.unit
def test_a_backend_with_no_cards_to_write_says_it_is_not_implemented(
    client, monkeypatch
):
    class _Other:
        def get_backend_name(self):
            return "gphoto2"

    _install(monkeypatch, _Other())
    api = _client_as(client, "boss", "admin")

    resp = api.post("/cameras/side/0", json={"side": "odd"})

    assert resp.status_code == 501, resp.text
    assert "gphoto2" in resp.json()["detail"]


@pytest.mark.unit
def test_something_that_is_not_a_parity_is_rejected_by_the_schema(
    client, monkeypatch
):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = _install(monkeypatch, make_backend(monkeypatch, make_pychdk(body)))
    backend.list_devices()
    api = _client_as(client, "boss", "admin")

    resp = api.post("/cameras/side/0", json={"side": "left"})

    assert resp.status_code == 422, resp.text
    assert body.uploads == []


@pytest.mark.unit
def test_the_device_list_carries_the_parity_and_the_reason_a_body_is_refused(
    client, monkeypatch
):
    first = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    second = Body(bus=1, address=7, serial="BBB222", card=b"EVEN\nid=cccccccccccc\n")
    _install(monkeypatch, make_backend(monkeypatch, make_pychdk(first, second)))
    api = _client_as(client, "rev", "reviewer")

    resp = api.get("/cameras/devices")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["side"] for row in body] == ["even", "even"]
    assert all("even" in row["error"] for row in body)
