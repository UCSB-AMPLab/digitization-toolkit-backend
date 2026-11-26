"""What the capture routes refuse before a page is taken: unset camera pages and pending colour card shots.

POST /cameras/capture and POST /cameras/capture/dual save a volume's pages,
whichever client sends them: the capture screen, a second browser, the
capture key or a script. Two rules the capture screen applies hold in the
routes too:

- While the cameras' pages are not set (GET /cameras/setup reports
  needs_setup), every capture is refused 409 `camera_setup_needed`, retakes
  and inserts included, and so is a capture for which the camera list cannot
  be read. The camera check's test captures (POST /cameras/test-capture/{i})
  are not refused: the camera step needs them.
- While a volume's colour card shots are due at its start (`start`) or once
  for a volume begun without them (`begun_without`), a new page is refused
  409 `colour_card_pending`, an insert included. A retake is not refused. A
  change of camera settings (`settings_changed`) stays a request the capture
  screen makes, and settings that cannot be read without the camera (an
  unknown answer) refuse nothing.

A repeated client_capture_id is answered from the capture it saved before
either check, so a replay is never refused.

The camera is stood in for as tests/unit/test_testing_space_capture.py does,
and the unit is that file's: a real project and volume with one real dual
capture, one user per role and the testing account.

    python -m pytest tests/unit/test_capture_refusals_set_up.py
"""

import json
import uuid

import pytest

import capture.service as capture_service
from tests.unit.test_capture_contract_naming import _FakeBackend
from tests.unit.test_colour_card import _SettingsBackend, _take_set, _volume
from tests.unit.test_testing_space_capture import (  # noqa: F401  (fixtures)
    backend, lookups, unit,
)


A = "canoneosrebelt7_1111111"
B = "canoneosrebelt7_2222222"
CONFIRMED = "2026-11-12T10:00:00+00:00"


def _rows():
    return [
        {"index": 0, "model": "Canon EOS Rebel T7", "hardware_id": A, "serial": "1111111",
         "location": "USB usb:001,004", "port": "usb:001,004", "usb_port": "1-1.2"},
        {"index": 1, "model": "Canon EOS Rebel T7", "hardware_id": B, "serial": "2222222",
         "location": "USB usb:001,005", "port": "usb:001,005", "usb_port": "1-1.3"},
    ]


class _RigBackend(_FakeBackend):
    """The fake camera, listing two bodies; `list_error` makes the listing fail."""

    def __init__(self):
        super().__init__()
        self.rows = _rows()
        self.list_error = None
        self.listings = 0

    def list_devices(self):
        self.listings += 1
        if self.list_error is not None:
            raise self.list_error
        return [dict(r) for r in self.rows]


def _record(role, confirmed_at, port):
    return {"model": "Canon EOS Rebel T7", "serial": None, "location": None, "machine_id": None,
            "label": None, "last_seen_index": 0, "first_registered_at": CONFIRMED,
            "last_seen_at": CONFIRMED, "calibration": {}, "orientation": None,
            "role": role, "role_confirmed_at": confirmed_at, "usb_port_at_confirmation": port}


def _write_roles(confirmed):
    """Both bodies with their roles, confirmed as a pair, or with no confirmation."""
    from capture.camera_registry import CameraRegistry

    stamp = CONFIRMED if confirmed else None
    data = {"version": "1.0",
            "cameras": {A: _record("left", stamp, "1-1.2"), B: _record("right", stamp, "1-1.3")}}
    if confirmed:
        data["confirmed_pair"] = {"left": A, "right": B}
    CameraRegistry().registry_path.write_text(json.dumps(data))


@pytest.fixture
def rig(unit, monkeypatch):
    """The unit with two bodies listed and their pages set and confirmed."""
    b = _RigBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    unit.backend = b
    _write_roles(confirmed=True)
    return unit


def _cid():
    return str(uuid.uuid4())


def _single(unit, volume_id, **extra):
    return unit.post("operator", "/cameras/capture", {
        "project_name": unit.real_project.name, "collection_id": volume_id, "camera_index": 0, **extra})


def _dual(unit, volume_id, **extra):
    return unit.post("operator", "/cameras/capture/dual", {
        "project_name": unit.real_project.name, "collection_id": volume_id, **extra})


def _ok(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True, resp.text
    return body


def _refused(resp, code):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code, resp.text
    assert detail["message"], resp.text
    return detail


def _counts(unit):
    from app.models.record import Record, RecordImage
    unit.db.expire_all()
    return unit.db.query(Record).count(), unit.db.query(RecordImage).count()


def _single_record(unit, volume_id):
    """A single-mode record in the volume, for a single retake."""
    return _ok(_single(unit, volume_id))["record_id"]


# ---------------------------------------------------------------------------
# NEH-427: the cameras' pages
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_with_the_pages_confirmed_every_capture_is_taken(rig):
    _ok(_single(rig, rig.real_volume.id))
    _ok(_dual(rig, rig.real_volume.id))
    _ok(_dual(rig, rig.real_volume.id, after_record_id=rig.real_record_id))
    _ok(_dual(rig, rig.real_volume.id, record_id=rig.real_record_id))


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["single", "dual", "insert", "single_retake", "dual_retake"])
def test_every_capture_is_refused_while_the_pages_are_not_set(rig, kind):
    single_id = _single_record(rig, rig.real_volume.id)
    _write_roles(confirmed=False)
    shots, counts = list(rig.backend.requested), _counts(rig)

    resp = {
        "single": lambda: _single(rig, rig.real_volume.id),
        "dual": lambda: _dual(rig, rig.real_volume.id),
        "insert": lambda: _dual(rig, rig.real_volume.id, after_record_id=rig.real_record_id),
        "single_retake": lambda: _single(rig, rig.real_volume.id, record_id=single_id),
        "dual_retake": lambda: _dual(rig, rig.real_volume.id, record_id=rig.real_record_id),
    }[kind]()

    detail = _refused(resp, "camera_setup_needed")
    assert detail["reasons"] == ["role_unconfirmed"]
    assert rig.backend.requested == shots, "a refused capture never reaches the camera"
    assert _counts(rig) == counts


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["single", "dual", "dual_retake"])
def test_a_capture_is_refused_when_the_camera_list_cannot_be_read(rig, kind):
    rig.backend.list_error = RuntimeError("PTP I/O error")
    shots, counts = list(rig.backend.requested), _counts(rig)

    resp = {
        "single": lambda: _single(rig, rig.real_volume.id),
        "dual": lambda: _dual(rig, rig.real_volume.id),
        "dual_retake": lambda: _dual(rig, rig.real_volume.id, record_id=rig.real_record_id),
    }[kind]()

    detail = _refused(resp, "camera_setup_needed")
    assert detail["reasons"] == ["camera_list_unreadable"]
    assert rig.backend.requested == shots
    assert _counts(rig) == counts


@pytest.mark.unit
def test_a_capture_is_refused_when_the_capture_system_cannot_be_reached(rig, monkeypatch):
    def unavailable():
        raise RuntimeError("no backend")

    monkeypatch.setattr(capture_service, "get_backend", unavailable)

    detail = _refused(_dual(rig, rig.real_volume.id), "camera_setup_needed")
    assert detail["reasons"] == ["camera_list_unreadable"]


@pytest.mark.unit
@pytest.mark.parametrize("route", ["single", "dual"])
def test_a_repeated_capture_already_saved_is_answered_while_the_pages_are_not_set(rig, route):
    cid = _cid()
    send = _single if route == "single" else _dual
    first = _ok(send(rig, rig.real_volume.id, client_capture_id=cid))
    _write_roles(confirmed=False)
    shots = list(rig.backend.requested)

    again = _ok(send(rig, rig.real_volume.id, client_capture_id=cid))

    assert again["replayed"] is True and again["record_id"] == first["record_id"]
    assert rig.backend.requested == shots


@pytest.mark.unit
def test_the_camera_check_still_takes_its_test_captures_while_the_pages_are_not_set(rig, monkeypatch):
    _write_roles(confirmed=False)
    monkeypatch.setattr(capture_service, "test_capture_bytes",
                        lambda idx, res, focus_check=False: (b"\xff\xd8fake", 0.1))

    resp = rig.post("operator", "/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert resp.content == b"\xff\xd8fake"


# ---------------------------------------------------------------------------
# NEH-425: colour card shots
# ---------------------------------------------------------------------------

@pytest.fixture
def card(unit, monkeypatch):
    """The unit with the colour card setting on and a CHDK-like backend (no bodies listed)."""
    from app.core import unit_mode
    b = _SettingsBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    unit.backend = b
    unit_mode.set_colour_card_shots(unit.db, True, actor="u-admin")
    return unit


def _setting(unit, on):
    from app.core import unit_mode
    unit_mode.set_colour_card_shots(unit.db, on, actor="u-admin")


@pytest.mark.unit
@pytest.mark.parametrize("route", ["single", "dual"])
def test_a_new_volume_takes_no_page_before_its_colour_card_shots(card, route):
    v = _volume(card)
    shots, counts = list(card.backend.requested), _counts(card)

    resp = (_single if route == "single" else _dual)(card, v.id)

    detail = _refused(resp, "colour_card_pending")
    assert detail["reason"] == "start"
    assert card.backend.requested == shots
    assert _counts(card) == counts


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["single", "dual", "insert"])
def test_a_volume_begun_without_its_shots_takes_no_new_page_until_they_are_answered(card, kind):
    # The unit's real volume was begun with the setting off.
    vid = card.real_volume.id
    resp = {
        "single": lambda: _single(card, vid),
        "dual": lambda: _dual(card, vid),
        "insert": lambda: _dual(card, vid, after_record_id=card.real_record_id),
    }[kind]()

    detail = _refused(resp, "colour_card_pending")
    assert detail["reason"] == "begun_without"


@pytest.mark.unit
def test_a_retake_is_taken_while_the_shots_are_pending(card):
    _setting(card, False)
    single_id = _single_record(card, card.real_volume.id)
    _setting(card, True)

    _ok(_dual(card, card.real_volume.id, record_id=card.real_record_id))
    _ok(_single(card, card.real_volume.id, record_id=single_id))


@pytest.mark.unit
def test_pages_are_taken_once_the_shots_are_taken_or_skipped(card):
    v = _volume(card)
    _take_set(card, v.id)
    _ok(_dual(card, v.id))

    skipped = card.post("operator", f"/collections/{card.real_volume.id}/colour-card/skip")
    assert skipped.status_code == 200, skipped.text
    _ok(_dual(card, card.real_volume.id))


@pytest.mark.unit
def test_a_change_of_camera_settings_refuses_no_page(card):
    v = _volume(card)
    _take_set(card, v.id)
    card.backend.zoom[1] = 5
    state = card.get("operator", f"/collections/{v.id}/colour-card").json()
    assert (state["needed"], state["reason"]) == (True, "settings_changed")

    _ok(_dual(card, v.id))
    _ok(_single(card, v.id))


@pytest.mark.unit
def test_settings_unknown_without_the_camera_refuse_no_page_and_the_camera_is_not_read(card):
    card.backend.name = "gphoto2"
    v = _volume(card)
    _take_set(card, v.id)
    reads = list(card.backend.reads)

    _ok(_dual(card, v.id))
    _ok(_single(card, v.id))

    assert card.backend.reads == reads, "the capture check reads no camera settings"


@pytest.mark.unit
@pytest.mark.parametrize("route", ["single", "dual"])
def test_a_repeated_capture_already_saved_is_answered_while_the_shots_are_pending(card, route):
    _setting(card, False)
    v = _volume(card)
    cid = _cid()
    send = _single if route == "single" else _dual
    first = _ok(send(card, v.id, client_capture_id=cid))
    _setting(card, True)
    shots = list(card.backend.requested)

    again = _ok(send(card, v.id, client_capture_id=cid))

    assert again["replayed"] is True and again["record_id"] == first["record_id"]
    assert card.backend.requested == shots


@pytest.mark.unit
def test_a_testing_volume_is_never_refused_for_colour_card_shots(card):
    project = card.project()
    volume = card.volume(project_id=project["id"])
    resp = card.post("testing", "/cameras/capture/dual",
                     {"project_name": project["name"], "collection_id": volume["id"]})
    _ok(resp)
