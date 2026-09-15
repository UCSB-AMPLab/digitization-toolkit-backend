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

import threading

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


def _run(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


@pytest.mark.unit
def test_two_assignments_cannot_both_take_the_same_parity(monkeypatch):
    """Both bodies are unassigned; both requests see odd free at the same time."""
    one = Body(bus=1, address=4, serial="AAA111", card=None)
    two = Body(bus=1, address=7, serial="BBB222", card=None)
    backend = make_backend(monkeypatch, make_pychdk(one, two))
    backend.list_devices()

    started = threading.Barrier(2, timeout=10)
    outcomes = []

    def assign(index):
        started.wait()
        try:
            backend.assign_side(index, "odd")
            outcomes.append(("ok", index))
        except RuntimeError as exc:
            outcomes.append(("refused", str(exc)))

    first = _run(lambda: assign(0))
    second = _run(lambda: assign(1))
    first.join(timeout=10)
    second.join(timeout=10)

    accepted = [entry for entry in outcomes if entry[0] == "ok"]
    assert len(accepted) == 1, f"both writes were accepted: {outcomes}"
    odd_cards = [
        body for body in (one, two)
        if body.card is not None and body.card.startswith(b"ODD")
    ]
    assert len(odd_cards) == 1, "two cards were written with the same parity"


@pytest.mark.unit
def test_assigning_a_parity_mints_a_fresh_id_for_a_duplicated_one(monkeypatch):
    """The route is the repair tool, so it has to be able to break the tie."""
    first = Body(bus=1, address=4, serial=None, card=b"EVEN\nid=aaaaaaaaaaaa\n")
    second = Body(bus=1, address=7, serial=None, card=b"ODD\nid=aaaaaaaaaaaa\n")
    backend = make_backend(monkeypatch, make_pychdk(first, second))
    assert all(row["error"] for row in backend.list_devices())

    rows = backend.assign_side(1, "odd")

    _remote, payload = second.uploads[-1]
    assert payload.startswith(b"ODD\n")
    assert b"id=aaaaaaaaaaaa" not in payload, "the duplicate id was kept"
    assert all(row["error"] is None for row in rows)
    assert len({row["hardware_id"] for row in rows}) == 2


@pytest.mark.unit
def test_a_card_id_the_clash_does_not_involve_is_left_alone(monkeypatch):
    """Body 0 collides through its USB serial, so its card id is not the fault.

    Minting over it would destroy a good identity and repair nothing: the
    body would still answer to the serial it shares.
    """
    from_serial = Body(bus=1, address=4, serial="cccccccccccc",
                       card=b"EVEN\nid=aaaaaaaaaaaa\n")
    from_card = Body(bus=1, address=7, serial=None,
                     card=b"ODD\nid=cccccccccccc\n")
    backend = make_backend(monkeypatch, make_pychdk(from_serial, from_card))
    backend.list_devices()

    rows = backend.assign_side(0, "even")

    _remote, payload = from_serial.uploads[-1]
    assert payload == b"EVEN\nid=aaaaaaaaaaaa\n", "a good card id was overwritten"
    assert all(row["error"] for row in rows), "the clash was reported as fixed"


@pytest.mark.unit
def test_assigning_to_the_repairable_body_ends_the_clash(monkeypatch):
    from_serial = Body(bus=1, address=4, serial="cccccccccccc",
                       card=b"EVEN\nid=aaaaaaaaaaaa\n")
    from_card = Body(bus=1, address=7, serial=None,
                     card=b"ODD\nid=cccccccccccc\n")
    backend = make_backend(monkeypatch, make_pychdk(from_serial, from_card))
    backend.list_devices()

    rows = backend.assign_side(1, "odd")

    _remote, payload = from_card.uploads[-1]
    assert b"id=cccccccccccc" not in payload, "the duplicated id was kept"
    assert all(row["error"] is None for row in rows)
    assert len({row["hardware_id"] for row in rows}) == 2


@pytest.mark.unit
def test_an_assignment_refuses_a_body_that_was_dropped_while_it_waited(monkeypatch, tmp_path):
    """A capture can fail, evict and close the body while the write queues."""
    from capture.camera import CameraConfig

    from .chdk_fakes import TransportError, park_at_lock

    body = Body(serial="AAA111", card=EVEN_CARD,
                shoot_error=TransportError("[Errno 19] No such device"))
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()
    held = park_at_lock(backend, 0)

    result = {}

    def assign():
        try:
            backend.assign_side(0, "odd")
            result["outcome"] = "written"
        except RuntimeError as exc:
            result["outcome"] = str(exc)

    writer = _run(assign)
    assert held.arrived.wait(10), "the assignment never reached the lock"

    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "page.jpg", CameraConfig(camera_index=0))
    assert body.closes == 1, "the capture did not drop the body"

    held.let_through()
    writer.join(timeout=10)

    assert "not connected" in result.get("outcome", ""), result
    assert body.uploads == [], "the card was written through a closed session"


@pytest.mark.unit
def test_no_capture_lands_between_the_card_write_and_the_new_layout(
    monkeypatch, tmp_path
):
    """The window that corrupts the record rather than failing.

    Once the card has been rewritten the body shoots the other parity, but
    until the rescan publishes, the old layout still says it holds the old
    index. A capture that ran in between would pass revalidation against that
    old layout and file its page under an index the body no longer has -
    silently, with nothing in the manifest to show for it.
    """
    from capture.camera import CameraConfig

    body = Body(serial="AAA111", card=EVEN_CARD, image=b"\xff\xd8\xff\xd9")
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()

    scanning = fake.gate_list()
    writer = _run(lambda: backend.assign_side(0, "odd"))
    scanning.wait_until_entered()
    assert body.uploads, "the card was not written before the rescan"

    result = {}

    def capture():
        try:
            backend.capture_image(
                tmp_path / "page.jpg", CameraConfig(camera_index=0)
            )
            result["outcome"] = "shot"
        except RuntimeError as exc:
            result["outcome"] = str(exc)

    shooter = _run(capture)
    shooter.join(timeout=1)

    scanning.release()
    writer.join(timeout=10)
    shooter.join(timeout=10)

    assert body.shots == [], "a page was shot against the layout being replaced"
    assert result.get("outcome", "").startswith("Camera 0"), result
    assert not (tmp_path / "page.jpg").exists()


@pytest.mark.unit
def test_a_rescan_that_fails_after_the_write_does_not_leave_the_old_layout(
    monkeypatch, tmp_path
):
    """The card has already changed, so the published layout is now a lie.

    Once A/OWN.TXT is written the body shoots the other parity, and the
    indices only catch up when the rescan publishes. If that rescan fails -
    the bus enumeration goes wrong, say - the old layout stays live and a
    capture is accepted and recorded under an index the body no longer has.
    Same wrong-camera outcome as the races, reached through an error path.
    """
    from capture.camera import CameraConfig

    from .chdk_fakes import TransportError

    body = Body(serial="AAA111", card=EVEN_CARD, image=b"\xff\xd8\xff\xd9")
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()

    fake.list_error = TransportError("[Errno 19] No such device")

    with pytest.raises(RuntimeError) as exc:
        backend.assign_side(0, "odd")

    assert body.uploads, "the test needs the card to have been written"
    assert "re-enumerated" in str(exc.value), exc.value
    assert "not connected" not in str(exc.value), "that would read as a 404"
    assert body.closes == 1, "the body stayed usable on a layout it had left"
    assert backend._body_at(0) is None

    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "page.jpg", CameraConfig(camera_index=0))
    assert body.shots == []
