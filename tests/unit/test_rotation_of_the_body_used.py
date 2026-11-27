"""A capture applies the rotation saved for the body that took the picture (NEH-404).

A capture request resolves the body at a position, and that body's saved
rotation, before it takes the camera's lock. A rescan in between can put
another body at the position, and the picture is then taken by that body.
The rotation is decided under the camera lock, from the identity of the body
the capture used: the backends that tell bodies apart (gphoto2, CHDK) report
that identity under the lock, and capture.project_manager.body_rotation reads
the rotation saved for it. A body whose identity cannot be read at capture
time never takes the rotation saved under another body's identity.
A body whose identity cannot be read uses the entry the registry keys by
position (the model_idxN-style id) for the position the capture used, when
there is one. A rotation the request names explicitly is applied as named.
Colour card shots and test captures follow the same rule.

The gphoto2 cases run the real GPhoto2Backend against the fake libgphoto2 of
test_gphoto2_stored_format.py; the CHDK case runs the real ChdkBackend
against chdk_fakes.

Version: 0.4.0
"""

import io
import json
import logging
import threading
from pathlib import Path

import piexif
import pytest
from PIL import Image

import capture.backends.gphoto2_backend as gb
import capture.project_manager as project_manager_module
import capture.service as capture_service
from capture.camera import CameraConfig
from capture.camera_registry import CameraRegistry

from .chdk_fakes import Body, make_backend, make_pychdk
from .test_gphoto2_stored_format import (  # noqa: F401 (fixtures)
    ABILITIES, HW_A, HW_B, HW_C, PORT_A, PORT_B, PORT_C, SERIAL_A, SERIAL_B,
    SERIAL_C, LOGGER, rig, system_log,
)


STEM = "co-test-rot404"
# The registry id the device rows give a body with no serial at index 0.
HW_IDX0 = "canoneos1500d_idx0"
# A body the registry records that is no longer connected.
HW_D = "canoneos1500d_4444444"
ORIENTATION_TAG = 0x0112
EXIF_FOR_DEG = {0: None, 90: 6, 180: 3, 270: 8}


def _jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (8, 4), "white").save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def registry(rig):
    """The registry at the default path, which body_rotation reads."""
    from app.core.config import settings

    return CameraRegistry(registry_path=Path(settings.PROJECTS_ROOT) / "cameras.json")


@pytest.fixture
def backend(rig, registry, system_log, monkeypatch):  # noqa: F811
    rig.shots = []

    def stub_capture(self, outpath, retry=None, retry_delay=None, focus_check=False):
        rig.shots.append(self.port)
        outpath.parent.mkdir(parents=True, exist_ok=True)
        outpath.write_bytes(_jpeg_bytes())
        return 0.0, outpath

    monkeypatch.setattr(gb._PTPSession, "capture", stub_capture)
    backend = gb.GPhoto2Backend(logging.getLogger(LOGGER))
    backend._registry_factory = lambda: CameraRegistry(registry_path=registry.registry_path)
    backend._system_log = lambda **row: system_log.append(row)
    yield backend
    backend.cleanup()


def _save(registry, hardware_id, degrees, index):
    registry.register_resolved(hardware_id, {"model": ABILITIES}, index)
    registry.update_orientation(hardware_id, degrees)


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection

    v = Collection(name="V404", project_id=test_project.id, filename_stem=STEM)
    db_session.add(v)
    db_session.commit()
    return v


@pytest.fixture
def api(client, monkeypatch, backend):
    from app.api import cameras as cameras_api
    from app.api.auth import get_current_user
    from app.main import app
    from app.models.user import User

    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "_backend", backend)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(cameras_api, "_refuse_a_capture_while_camera_setup_is_needed", lambda: None)
    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x",
        role="operator", is_active=True,
    )
    return client


def _resolve_then(monkeypatch, resolved, between=None):
    """The request resolves `resolved` (index -> rotation), then `between` runs.

    `between` stands for whatever happens after the request has read the
    rotation of the body at a position and before the capture takes that
    camera's lock: here, a rescan that replaces a body.
    """
    calls = []

    def fake_default(camera_index, resolution="high", registry=None):
        calls.append(camera_index)
        config = {"camera_index": camera_index, "rotate_deg": resolved[camera_index]}
        if between is not None and len(calls) == len(resolved):
            between()
        return config, "resolved-at-request"

    monkeypatch.setattr(project_manager_module, "default_camera_config_from_registry", fake_default)


def _replace_a_by_c(rig, backend):  # noqa: F811
    """Body A leaves; a rescan puts body C at index 0. B stays at index 1."""
    rig.detected = [(ABILITIES, PORT_B)]
    del rig.bodies[PORT_A]
    rig.add(PORT_C, SERIAL_C)
    backend.rescan()
    assert backend._pins == {0: SERIAL_C, 1: SERIAL_B}


def _post(api, path, payload):
    resp = api.post(path, json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"], resp.json()
    return resp.json()


def _images(db_session, record_id):
    from app.models.record import RecordImage

    db_session.expire_all()
    return (db_session.query(RecordImage).filter(RecordImage.record_id == record_id)
            .order_by(RecordImage.id).all())


def _manifest_files(image_path):
    """The `files` of the last capture record in the manifest of the image's volume."""
    manifest = Path(image_path).parent.parent / "metadata" / "manifest.jsonl"
    lines = manifest.read_text().splitlines()
    return json.loads([line for line in lines if line.strip()][-1])["files"]


def _orientation(path):
    return piexif.load(str(path))["0th"].get(ORIENTATION_TAG)


def _by_camera(images):
    return {("c0" if "_c0" in i.filename else "c1"): i for i in images}


# --- single capture -------------------------------------------------------------


@pytest.mark.unit
def test_a_body_replaced_before_the_lock_gives_the_image_its_own_rotation(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry
):
    """NEH-404: A saved at 270, C at 90; C replaces A at position 0 between
    the request's resolution and the lock. The image is stored at 90."""
    rig.add(PORT_A, SERIAL_A)
    rig.add(PORT_B, SERIAL_B)
    _save(registry, HW_A, 270, 0)
    _save(registry, HW_B, 180, 1)
    _save(registry, HW_C, 90, 2)
    backend.list_devices()
    _resolve_then(monkeypatch, {0: 270}, lambda: _replace_a_by_c(rig, backend))

    out = _post(api, "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    [img] = _images(db_session, out["record_id"])
    assert img.rotate_deg == 90
    assert [f["rotate_deg"] for f in _manifest_files(out["file_path"])] == [90]
    assert _orientation(out["file_path"]) == EXIF_FOR_DEG[90]


@pytest.mark.unit
def test_a_body_left_in_place_keeps_its_saved_rotation(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A)
    rig.add(PORT_B, SERIAL_B)
    _save(registry, HW_A, 270, 0)
    _save(registry, HW_B, 180, 1)
    backend.list_devices()
    _resolve_then(monkeypatch, {0: 270})

    out = _post(api, "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    [img] = _images(db_session, out["record_id"])
    assert img.rotate_deg == 270
    assert [f["rotate_deg"] for f in _manifest_files(out["file_path"])] == [270]
    assert _orientation(out["file_path"]) == EXIF_FOR_DEG[270]


@pytest.mark.unit
def test_a_rotation_the_request_names_is_applied_whatever_body_takes_the_picture(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry,
):
    rig.add(PORT_A, SERIAL_A)
    rig.add(PORT_B, SERIAL_B)
    _save(registry, HW_A, 270, 0)
    _save(registry, HW_C, 90, 2)
    backend.list_devices()
    _resolve_then(monkeypatch, {0: 270}, lambda: _replace_a_by_c(rig, backend))

    out = _post(api, "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0,
                 "collection_id": volume.id, "rotate_deg": 180})

    [img] = _images(db_session, out["record_id"])
    assert img.rotate_deg == 180


@pytest.mark.unit
def test_a_body_whose_identity_cannot_be_read_takes_the_default_not_the_resolved_rotation(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry,
):
    """No serial and no entry for the position: the rotation resolved at
    request time (270) is not applied; the body takes 90."""
    rig.add(PORT_A, None)
    backend.list_devices()
    _resolve_then(monkeypatch, {0: 270})

    out = _post(api, "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    [img] = _images(db_session, out["record_id"])
    assert img.rotate_deg == 90


@pytest.mark.unit
def test_a_serial_less_body_with_a_positional_entry_keeps_its_rotation(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry,
):
    """A positional id names the position: its saved 270 applies to the
    body with no serial that captures at that position."""
    rig.add(PORT_A, None)
    backend.list_devices()
    _save(registry, HW_IDX0, 270, 0)
    _resolve_then(monkeypatch, {0: 0})

    out = _post(api, "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    [img] = _images(db_session, out["record_id"])
    assert img.rotate_deg == 270
    assert _orientation(out["file_path"]) == EXIF_FOR_DEG[270]


@pytest.mark.unit
def test_the_entry_of_a_body_no_longer_there_is_not_applied(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry,
):
    """The registry still records body D, saved at 270 under its identity,
    at position 0, and the request resolves D's 270. D is gone: the body at
    position 0 answers no serial. D's entry is not applied; with no
    positional entry the body takes 90."""
    rig.add(PORT_A, None)
    _save(registry, HW_D, 270, 0)
    backend.list_devices()
    _resolve_then(monkeypatch, {0: 270})

    out = _post(api, "/cameras/capture",
                {"project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    [img] = _images(db_session, out["record_id"])
    assert img.rotate_deg == 90
    assert _orientation(out["file_path"]) == EXIF_FOR_DEG[90]


# --- dual capture ---------------------------------------------------------------


@pytest.mark.unit
def test_a_dual_capture_with_one_body_replaced_gives_each_image_its_bodys_rotation(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A)
    rig.add(PORT_B, SERIAL_B)
    _save(registry, HW_A, 270, 0)
    _save(registry, HW_B, 180, 1)
    _save(registry, HW_C, 90, 2)
    backend.list_devices()
    _resolve_then(monkeypatch, {0: 270, 1: 180}, lambda: _replace_a_by_c(rig, backend))

    out = _post(api, "/cameras/capture/dual",
                {"project_name": test_project.name, "collection_id": volume.id})

    images = _by_camera(_images(db_session, out["record_id"]))
    assert images["c0"].rotate_deg == 90, "C took camera 0's picture"
    assert images["c1"].rotate_deg == 180, "B stayed at camera 1"
    files = {f["role"]: f["rotate_deg"] for f in _manifest_files(out["file_paths"][0])}
    assert files == {"left": 90, "right": 180}
    for img in images.values():
        assert _orientation(img.file_path) == EXIF_FOR_DEG[img.rotate_deg]


@pytest.mark.unit
@pytest.mark.parametrize("identified_at", [0, 1])
def test_a_dual_capture_of_an_identified_and_an_unidentified_body_gives_each_its_own_rotation(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry, identified_at,
):
    """One body answers a serial (its entry: 270), the other none (the
    positional entry for its index: 180). Each image carries its own body's
    rotation: the unidentified one never takes the identified one's."""
    unidentified_at = 1 - identified_at
    ports = {identified_at: (PORT_A, SERIAL_A), unidentified_at: (PORT_B, None)}
    for index in (0, 1):
        rig.add(*ports[index])
    backend.list_devices()
    layout = {i: p for i, (_m, p) in backend._port_map.items()}
    assert layout == {i: ports[i][0] for i in (0, 1)}, layout
    _save(registry, HW_A, 270, identified_at)
    _save(registry, f"canoneos1500d_idx{unidentified_at}", 180, unidentified_at)
    _resolve_then(monkeypatch, {0: 0, 1: 0})

    out = _post(api, "/cameras/capture/dual",
                {"project_name": test_project.name, "collection_id": volume.id})

    want = {f"c{identified_at}": 270, f"c{unidentified_at}": 180}
    images = _by_camera(_images(db_session, out["record_id"]))
    assert {k: i.rotate_deg for k, i in images.items()} == want
    for key, img in images.items():
        assert _orientation(img.file_path) == EXIF_FOR_DEG[want[key]], key
    role_of = {"c0": "left", "c1": "right"}
    files = {f["role"]: f["rotate_deg"] for f in _manifest_files(out["file_paths"][0])}
    assert files == {role_of[k]: v for k, v in want.items()}


# --- the backends report the body they hold under the lock --------------------------


def _held_by_another_thread_check(lock):
    """True when `lock` is held: another thread cannot take it without waiting."""
    taken = []

    def try_it():
        got = lock.acquire(blocking=False)
        taken.append(got)
        if got:
            lock.release()

    t = threading.Thread(target=try_it)
    t.start()
    t.join()
    return not taken[0]


@pytest.mark.unit
def test_gphoto2_reports_the_body_its_session_holds_under_the_lock_before_the_shutter(
    tmp_path, rig, backend, registry,
):
    rig.add(PORT_A, SERIAL_A)
    rig.add(PORT_B, SERIAL_B)
    backend.list_devices()
    _replace_a_by_c(rig, backend)
    seen = []

    def body_identified(hardware_id, position_id=None):
        seen.append((hardware_id, position_id,
                     _held_by_another_thread_check(backend._get_camera_lock(0)),
                     list(rig.shots)))

    backend.capture_image(tmp_path / "cam0.jpg", CameraConfig(camera_index=0),
                          body_identified=body_identified)

    assert seen == [(HW_C, None, True, [])], "under the lock, before any shutter"
    assert rig.shots == [PORT_C]


@pytest.mark.unit
def test_gphoto2_a_callback_that_raises_fails_the_capture_before_the_shutter(
    tmp_path, rig, backend, registry,
):
    rig.add(PORT_A, SERIAL_A)
    backend.list_devices()

    def body_identified(hardware_id, position_id=None):
        raise RuntimeError("the registry could not be read")

    with pytest.raises(RuntimeError, match="the registry could not be read"):
        backend.capture_image(tmp_path / "cam0.jpg", CameraConfig(camera_index=0),
                              body_identified=body_identified)

    assert rig.shots == [], "no exposure"
    assert not _held_by_another_thread_check(backend._get_camera_lock(0)), "lock released"
    assert not (tmp_path / "cam0.jpg").exists()


@pytest.mark.unit
def test_a_rotation_that_cannot_be_read_fails_the_capture_through_the_route(
    api, db_session, test_project, volume, monkeypatch, rig, backend, registry,
):
    from app.models.record import RecordImage

    rig.add(PORT_A, SERIAL_A)
    backend.list_devices()
    _resolve_then(monkeypatch, {0: 270})

    def unreadable(*args, **kwargs):
        raise RuntimeError("the registry could not be read")

    monkeypatch.setattr(project_manager_module, "body_rotation", unreadable)

    resp = api.post("/cameras/capture", json={
        "project_name": test_project.name, "camera_index": 0, "collection_id": volume.id})

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is False
    assert "the registry could not be read" in resp.json()["error"]
    assert rig.shots == []
    db_session.expire_all()
    assert db_session.query(RecordImage).count() == 0


@pytest.mark.unit
def test_gphoto2_reports_the_position_for_a_body_with_no_serial(tmp_path, rig, backend):
    rig.add(PORT_A, None)
    backend.list_devices()
    seen = []

    backend.capture_image(tmp_path / "cam0.jpg", CameraConfig(camera_index=0),
                          body_identified=lambda *args: seen.append(args))

    assert seen == [(None, HW_IDX0)]


# --- colour card shots and test captures --------------------------------------------


@pytest.mark.unit
def test_a_colour_card_shot_takes_the_rotation_of_the_body_that_took_it(
    monkeypatch, tmp_path, rig, backend, registry,
):
    rig.add(PORT_A, SERIAL_A)
    rig.add(PORT_B, SERIAL_B)
    _save(registry, HW_A, 270, 0)
    _save(registry, HW_C, 90, 2)
    backend.list_devices()
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    config = CameraConfig(camera_index=0, rotate_deg=270)  # resolved for A
    _replace_a_by_c(rig, backend)

    paths, _metadata = capture_service.colour_card_capture(config, tmp_path / "card.jpg")

    assert config.rotate_deg == 90
    assert _orientation(paths[0]) == EXIF_FOR_DEG[90]


@pytest.mark.unit
def test_a_test_capture_takes_the_rotation_of_the_body_that_took_it(
    monkeypatch, rig, backend, registry,
):
    rig.add(PORT_A, SERIAL_A)
    rig.add(PORT_B, SERIAL_B)
    _save(registry, HW_A, 270, 0)
    _save(registry, HW_C, 90, 2)
    backend.list_devices()
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    _resolve_then(monkeypatch, {0: 270}, lambda: _replace_a_by_c(rig, backend))

    image, _elapsed = capture_service.test_capture_bytes(0)

    assert piexif.load(image)["0th"].get(ORIENTATION_TAG) == EXIF_FOR_DEG[90]


def _chdk_replaced(monkeypatch):
    """A CHDK backend whose body at index 0 is C, after A left and a rescan."""
    a = Body(serial="AAA111", card=b"EVEN\nid=aaaaaaaaaaaa\n", image=_jpeg_bytes())
    fake = make_pychdk(a)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()
    c = Body(serial="CCC333", card=b"EVEN\nid=cccccccccccc\n", image=_jpeg_bytes(),
             address=a.address + 7)
    fake.bodies = [c]
    rows = backend.rescan()
    [row] = [r for r in rows if r["index"] == 0]
    assert row["serial"] == "CCC333"
    return backend, c, row


@pytest.mark.unit
def test_chdk_reports_the_body_it_holds_under_the_lock_before_the_shutter(monkeypatch, tmp_path):
    backend, c, row = _chdk_replaced(monkeypatch)
    seen = []

    def body_identified(hardware_id, position_id=None):
        body = backend._body_at(0)
        seen.append((hardware_id, position_id,
                     _held_by_another_thread_check(body.lock), list(c.shots)))

    backend.capture_image(tmp_path / "page.jpg", CameraConfig(camera_index=0),
                          body_identified=body_identified)

    assert seen == [(row["hardware_id"], None, True, [])], "under the lock, before any shutter"
    assert len(c.shots) == 1, "C took the picture"


@pytest.mark.unit
def test_chdk_a_callback_that_raises_fails_the_capture_before_the_shutter(monkeypatch, tmp_path):
    backend, c, _row = _chdk_replaced(monkeypatch)

    def body_identified(hardware_id, position_id=None):
        raise RuntimeError("the registry could not be read")

    with pytest.raises(RuntimeError, match="the registry could not be read"):
        backend.capture_image(tmp_path / "page.jpg", CameraConfig(camera_index=0),
                              body_identified=body_identified)

    assert c.shots == [], "no exposure"
    assert not _held_by_another_thread_check(backend._body_at(0).lock), "lock released"
    assert not (tmp_path / "page.jpg").exists()
