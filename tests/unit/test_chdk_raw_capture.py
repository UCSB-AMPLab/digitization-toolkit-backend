"""A CHDK capture in RAW is stored as a T7's RAW capture is, with `.dng` for `.cr2`.

pychdk's shoot(stream=True, dng=True, jpeg=True) returns the DNG and the
camera's own JPEG of the same exposure. The DNG is the master, written as
`<name>.dng` in place of the name the capture service chose; the JPEG is its
`_preview.jpg` sidecar, where app.core.paths.preview_sidecar_of puts it. A
CHDK DNG carries only a 128x96 thumbnail, so the preview is the camera's
JPEG and nothing in it is taken from the DNG.

The format is a per-capture choice on CHDK. The settings route stores it per
body in the backend, and the record capture routes put it in the capture's
CameraConfig. Test captures, the focus check and the live preview build
their own configuration and stay on JPEG.

Run with: python -m pytest tests/unit/test_chdk_raw_capture.py
"""

from pathlib import Path

import piexif
import pytest

import capture.backends.chdk_backend as cb
import capture.project_manager as project_manager_module
import capture.service as capture_service
from app.core.paths import preview_sidecar_of
from capture.camera import CameraConfig
from capture.camera_registry import CameraRegistry

from .chdk_fakes import Body, DngError, make_backend, make_pychdk
from .test_chdk_whole_captures import _canon_jpeg


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
CLEAR_CAPTURE = "init_usb_capture(0)"
# A DNG as the library assembles it: a little-endian TIFF header, then
# enough bytes to stand for the thumbnail and the raw data.
DNG = b"II*\x00\x08\x00\x00\x00" + bytes(2_000_000)
ORIENTATION_TAG = 0x0112


def _config(image_format=None, index=0, **extra):
    return CameraConfig(camera_index=index, image_format=image_format, **extra)


def _backend(monkeypatch, body):
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake, logger_name="test-chdk-raw")
    backend.list_devices()
    return backend, fake


def _raw_body(**kwargs):
    kwargs.setdefault("image", _canon_jpeg(1490830))
    kwargs.setdefault("dng", DNG)
    return Body(serial="AAA111", card=EVEN_CARD, **kwargs)


def _exif_jpeg():
    """A small JPEG whose EXIF piexif can rewrite, as an orientation tag needs."""
    import io

    from PIL import Image

    out = io.BytesIO()
    exif = piexif.dump({"0th": {piexif.ImageIFD.Make: b"Canon"}})
    Image.new("RGB", (16, 16), "white").save(out, "JPEG", exif=exif)
    return out.getvalue()


def _backend_scripts(body):
    return [s["script"] for s in body.scripts if s["flags"] is not None]


# --- the stored files ------------------------------------------------------


@pytest.mark.unit
def test_a_raw_capture_stores_the_dng_and_the_cameras_jpeg_as_its_preview(
    monkeypatch, tmp_path
):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)

    path, metadata = backend.capture_image(tmp_path / "page_c0.jpg", _config("RAW+JPEG"))

    master = tmp_path / "page_c0.dng"
    assert path == str(master)
    assert metadata is None
    assert master.read_bytes() == DNG
    preview = preview_sidecar_of(master)
    assert preview == tmp_path / "page_c0_preview.jpg"
    assert preview.read_bytes() == body.image, "the preview is the camera's JPEG"
    assert not (tmp_path / "page_c0.jpg").exists()
    assert body.shots[0]["stream"] is True
    assert body.shots[0]["dng"] is True
    assert body.shots[0]["jpeg"] is True


@pytest.mark.unit
def test_a_raw_capture_in_a_volume_puts_the_preview_in_previews(monkeypatch, tmp_path):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)
    captures = tmp_path / "vol" / "captures"
    captures.mkdir(parents=True)
    (tmp_path / "vol" / "previews").mkdir()

    path, _ = backend.capture_image(captures / "vol-img_0001_c0.jpg", _config("RAW+JPEG"))

    assert path == str(captures / "vol-img_0001_c0.dng")
    preview = tmp_path / "vol" / "previews" / "vol-img_0001_c0_preview.jpg"
    assert preview.read_bytes() == body.image


@pytest.mark.unit
def test_a_raw_capture_passes_shutter_and_iso(monkeypatch, tmp_path):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(
        tmp_path / "p.jpg", _config("RAW+JPEG", shutter_speed="1/250", iso=400)
    )

    assert body.shots[0]["shutter_speed"] == pytest.approx(1 / 250)
    assert body.shots[0]["market_iso"] == 400


@pytest.mark.unit
@pytest.mark.parametrize("image_format", [None, "JPEG"])
def test_jpeg_or_no_format_captures_a_jpeg_as_before(monkeypatch, tmp_path, image_format):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)

    path, _ = backend.capture_image(tmp_path / "p.jpg", _config(image_format))

    assert path == str(tmp_path / "p.jpg")
    assert Path(path).read_bytes() == body.image
    assert "dng" not in body.shots[0]
    assert "jpeg" not in body.shots[0]
    assert not (tmp_path / "p_preview.jpg").exists()


@pytest.mark.unit
def test_a_format_chdk_does_not_take_is_refused_before_anything_reaches_the_camera(
    monkeypatch, tmp_path
):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)
    sent_before = list(body.scripts)

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "p.jpg", _config("TIFF"))

    assert "TIFF" in str(exc.value)
    assert body.shots == []
    assert body.scripts == sent_before
    assert list(tmp_path.iterdir()) == []


@pytest.mark.unit
def test_raw_on_the_pinned_library_names_the_version_it_needs(monkeypatch, tmp_path):
    body = _raw_body(old_library=True)
    backend, _ = _backend(monkeypatch, body)
    sent_before = list(body.scripts)

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "p.jpg", _config("RAW+JPEG"))

    assert f"pychdk {cb._PYCHDK_DNG_VERSION}" in str(exc.value)
    assert body.shots == []
    assert body.scripts == sent_before, "nothing is sent to the camera"
    assert list(tmp_path.iterdir()) == []
    # The body is not dropped: a JPEG capture still works on it.
    path, _ = backend.capture_image(tmp_path / "p.jpg", _config("JPEG"))
    assert Path(path).read_bytes() == body.image


# --- whole-file checks -----------------------------------------------------


def _assert_refused_and_drained(body, tmp_path, exc):
    assert "not saved" in str(exc.value)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
    assert CLEAR_CAPTURE in _backend_scripts(body), "the body was drained"


@pytest.mark.unit
def test_a_dng_without_a_tiff_signature_is_refused(monkeypatch, tmp_path):
    body = _raw_body(dng=b"\x00" * len(DNG))
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(cb.IncompleteCaptureError) as exc:
        backend.capture_image(tmp_path / "p.jpg", _config("RAW+JPEG"))

    _assert_refused_and_drained(body, tmp_path, exc)
    assert "TIFF" in str(exc.value)


@pytest.mark.unit
def test_a_dng_holding_little_more_than_a_header_is_refused(monkeypatch, tmp_path):
    # A CHDK header (1,344 bytes) and a 128x96 RGB thumbnail come to about
    # 38 KB; the raw data of any sensor is far larger.
    body = _raw_body(dng=b"II*\x00\x08\x00\x00\x00" + bytes(38_200))
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(cb.IncompleteCaptureError) as exc:
        backend.capture_image(tmp_path / "p.jpg", _config("RAW+JPEG"))

    _assert_refused_and_drained(body, tmp_path, exc)


@pytest.mark.unit
def test_a_raw_capture_whose_jpeg_is_not_whole_stores_nothing(monkeypatch, tmp_path):
    body = _raw_body(image=_canon_jpeg(1490830)[:-2])
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(cb.IncompleteCaptureError) as exc:
        backend.capture_image(tmp_path / "p.jpg", _config("RAW+JPEG"))

    _assert_refused_and_drained(body, tmp_path, exc)
    assert "end marker" in str(exc.value)


@pytest.mark.unit
def test_a_dng_the_library_cannot_assemble_is_refused_and_drained(monkeypatch, tmp_path):
    body = _raw_body(
        shoot_error=DngError("The raw data is 10 bytes; the header declares 24724224")
    )
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(cb.IncompleteCaptureError) as exc:
        backend.capture_image(tmp_path / "p.jpg", _config("RAW+JPEG"))

    _assert_refused_and_drained(body, tmp_path, exc)
    assert "24724224" in str(exc.value)
    assert backend._body_at(0) is not None, "a drained body is kept"


@pytest.mark.unit
def test_the_preview_is_written_before_the_master(monkeypatch, tmp_path):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)
    written = []
    real = cb.atomic_write

    def recording(dest, write):
        real(dest, write)
        written.append(Path(dest).name)

    monkeypatch.setattr(cb, "atomic_write", recording)

    backend.capture_image(tmp_path / "p.jpg", _config("RAW+JPEG"))

    assert written == ["p_preview.jpg", "p.dng"]


@pytest.mark.unit
def test_a_master_that_cannot_be_written_takes_its_preview_with_it(
    monkeypatch, tmp_path
):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)
    real = cb.atomic_write

    def failing_on_master(dest, write):
        if Path(dest).suffix == ".dng":
            raise OSError(28, "No space left on device")
        real(dest, write)

    monkeypatch.setattr(cb, "atomic_write", failing_on_master)

    with pytest.raises(OSError):
        backend.capture_image(tmp_path / "p.jpg", _config("RAW+JPEG"))

    assert list(tmp_path.iterdir()) == []


# --- rotation --------------------------------------------------------------


@pytest.mark.unit
def test_rotation_tags_the_preview_and_leaves_the_dng_as_the_camera_sent_it(
    monkeypatch, tmp_path
):
    body = _raw_body(image=_exif_jpeg())
    backend, _ = _backend(monkeypatch, body)
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)

    path, _ = capture_service.capture_image(
        "proj", _config("RAW+JPEG", rotate_deg=90),
        output_filename="vol-img_0001_c0.jpg", check_camera=False,
        stem="vol", root=tmp_path,
    )

    master = Path(path)
    assert master.suffix == ".dng"
    assert master.parent.name == "captures"
    assert master.read_bytes() == DNG, "the DNG is never modified"
    preview = preview_sidecar_of(master)
    assert preview.parent.name == "previews"
    assert piexif.load(str(preview))["0th"].get(ORIENTATION_TAG) == 6


# --- capabilities and the stored format ------------------------------------


@pytest.mark.unit
def test_capabilities_offer_jpeg_and_raw(monkeypatch):
    backend, _ = _backend(monkeypatch, _raw_body())

    caps = backend.get_capabilities()

    assert caps["image_formats"] == ["JPEG", "RAW+JPEG"]
    assert caps["dslr_settings"] is False


@pytest.mark.unit
def test_the_format_is_stored_per_body_and_sends_nothing(monkeypatch):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)
    sent_before = list(body.scripts)

    assert backend.capture_image_format(0) is None
    assert backend.get_dslr_settings(0)["image_format"] == "JPEG"

    updated = backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    assert updated["image_format"] == "RAW+JPEG"
    assert updated["iso"] is None
    assert backend.capture_image_format(0) == "RAW+JPEG"
    assert backend.get_dslr_settings(0)["image_format"] == "RAW+JPEG"
    assert body.scripts == sent_before


@pytest.mark.unit
def test_the_stored_format_survives_the_body_being_dropped(monkeypatch):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)
    backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    backend.cleanup()
    backend.list_devices()

    assert backend.capture_image_format(0) == "RAW+JPEG"


HW_ID = "canon325b_AAA111"


def _registry(tmp_path):
    return CameraRegistry(registry_path=tmp_path / "cameras.json")


def _registry_backend(monkeypatch, tmp_path, body=None):
    fake = make_pychdk(body or _raw_body())
    backend = make_backend(
        monkeypatch, fake, logger_name="test-chdk-raw",
        registry=_registry(tmp_path),
    )
    backend.list_devices()
    return backend


def _store(registry, **values):
    """Write values into the registry the way a hand-edited file would."""
    registry.register_resolved(HW_ID, {"model": "A2500"}, 0)

    def change(cameras):
        cameras["cameras"][HW_ID].update(values)

    registry._mutate(change)


@pytest.mark.unit
def test_the_format_is_written_to_the_registry_under_the_bodys_identity(
    monkeypatch, tmp_path
):
    backend = _registry_backend(monkeypatch, tmp_path)

    backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    entry = _registry(tmp_path).get_camera_by_id(HW_ID)
    assert entry["image_format"] == "RAW+JPEG"
    assert "image_format_updated_at" in entry


@pytest.mark.unit
def test_the_format_survives_a_backend_restart(monkeypatch, tmp_path):
    first = _registry_backend(monkeypatch, tmp_path)
    first.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})
    first.cleanup()

    second = _registry_backend(monkeypatch, tmp_path)

    assert second.capture_image_format(0) == "RAW+JPEG"
    assert second.get_dslr_settings(0)["image_format"] == "RAW+JPEG"


@pytest.mark.unit
@pytest.mark.parametrize("stored", ["TIFF", "raw", 1, None, ["RAW"]])
def test_a_stored_format_that_is_not_one_chdk_takes_is_ignored(
    monkeypatch, tmp_path, caplog, stored
):
    import logging

    _store(_registry(tmp_path), image_format=stored)
    caplog.set_level(logging.WARNING, logger="test-chdk-raw")

    backend = _registry_backend(monkeypatch, tmp_path)

    assert backend.capture_image_format(0) is None
    assert backend.get_dslr_settings(0)["image_format"] == "JPEG"
    if stored is not None:
        assert any(
            "stored capture format" in r.getMessage()
            for r in caplog.records if r.levelno == logging.WARNING
        )


@pytest.mark.unit
def test_an_unreadable_registry_leaves_the_format_absent(monkeypatch, tmp_path, caplog):
    import logging

    fake = make_pychdk(_raw_body())
    backend = make_backend(monkeypatch, fake, logger_name="test-chdk-raw")

    def broken():
        raise OSError("cameras.json is not readable")

    backend._registry_factory = broken
    caplog.set_level(logging.WARNING, logger="test-chdk-raw")
    backend.list_devices()

    assert backend.capture_image_format(0) is None
    assert any(
        "stored capture format" in r.getMessage()
        for r in caplog.records if r.levelno == logging.WARNING
    )


@pytest.mark.unit
def test_the_registry_refuses_a_format_chdk_does_not_take(tmp_path):
    registry = _registry(tmp_path)
    registry.register_resolved(HW_ID, {"model": "A2500"}, 0)

    with pytest.raises(ValueError):
        registry.update_image_format(HW_ID, "TIFF")
    with pytest.raises(KeyError):
        registry.update_image_format("canon325b_nobody", "RAW")


@pytest.mark.unit
@pytest.mark.parametrize("settings", [
    {"image_format": "TIFF"},
    {"iso": "400"},
])
def test_a_setting_chdk_does_not_take_is_refused(monkeypatch, settings):
    backend, _ = _backend(monkeypatch, _raw_body())

    with pytest.raises(RuntimeError):
        backend.apply_dslr_settings(0, settings)

    assert backend.capture_image_format(0) is None


@pytest.mark.unit
def test_a_body_with_no_identity_cannot_hold_a_format(monkeypatch):
    body = Body(serial=None, card=b"EVEN\n", image=_canon_jpeg(1), dng=DNG)
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(RuntimeError):
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})


# --- the routes ------------------------------------------------------------


def _user(username, role):
    from app.models.user import User

    return User(
        username=username, email=f"{username}@example.com",
        hashed_password="x", role=role, is_active=True,
    )


def _client_as(client, username="op", role="operator"):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


@pytest.fixture
def installed(monkeypatch, override_projects_root):
    monkeypatch.setattr(
        project_manager_module, "default_camera_config_from_registry",
        lambda camera_index, resolution="high", registry=None: (
            {"camera_index": camera_index}, "hw"),
    )
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "_backend", backend)
    return backend, body


@pytest.mark.unit
def test_the_settings_route_stores_raw_for_a_chdk_camera(client, installed):
    backend, _ = installed
    api = _client_as(client)

    resp = api.put("/cameras/dslr/0/settings", json={"image_format": "RAW+JPEG"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_format"] == "RAW+JPEG"
    assert api.get("/cameras/dslr/0/settings").json()["image_format"] == "RAW+JPEG"
    assert backend.capture_image_format(0) == "RAW+JPEG"


@pytest.mark.unit
@pytest.mark.parametrize("payload", [
    {"image_format": "TIFF"},
    {"iso": "400"},
    {"image_format": "RAW", "shutter_speed": "1/125"},
])
def test_the_settings_route_refuses_what_a_chdk_camera_does_not_take(
    client, installed, payload
):
    backend, _ = installed

    resp = _client_as(client).put("/cameras/dslr/0/settings", json=payload)

    assert resp.status_code == 422, resp.text
    assert backend.capture_image_format(0) is None


@pytest.mark.unit
def test_a_test_capture_stays_on_jpeg_when_raw_is_stored(client, installed):
    backend, body = installed
    backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    resp = _client_as(client).post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert resp.content == body.image
    assert "dng" not in body.shots[-1]


@pytest.mark.unit
def test_a_focus_check_stays_on_jpeg_when_raw_is_stored(client, installed):
    backend, body = installed
    backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    resp = _client_as(client).post("/cameras/test-capture/0?focus_check=true")

    assert resp.status_code == 200, resp.text
    assert "dng" not in body.shots[-1]


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection

    v = Collection(name="Tomo 1", project_id=test_project.id, filename_stem="co-test-raw")
    db_session.add(v)
    db_session.commit()
    return v


@pytest.mark.unit
def test_a_record_capture_with_raw_stored_records_the_dng(
    client, db_session, test_project, volume, installed
):
    backend, body = installed
    api = _client_as(client)
    assert api.put(
        "/cameras/dslr/0/settings", json={"image_format": "RAW+JPEG"}
    ).status_code == 200

    resp = api.post("/cameras/capture", json={
        "project_name": test_project.name, "camera_index": 0,
        "collection_id": volume.id,
    })

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["success"], out
    master = Path(out["file_path"])
    assert master.suffix == ".dng"
    assert master.read_bytes() == DNG
    assert preview_sidecar_of(master).read_bytes() == body.image
    assert body.shots[-1]["dng"] is True


@pytest.mark.unit
def test_a_record_capture_with_nothing_stored_stays_on_jpeg(
    client, db_session, test_project, volume, installed
):
    _, body = installed

    resp = _client_as(client).post("/cameras/capture", json={
        "project_name": test_project.name, "camera_index": 0,
        "collection_id": volume.id,
    })

    assert resp.status_code == 200, resp.text
    assert Path(resp.json()["file_path"]).suffix == ".jpg"
    assert "dng" not in body.shots[-1]


# --- the old name RAW ------------------------------------------------------


@pytest.mark.unit
def test_raw_is_the_old_name_for_raw_plus_jpeg_at_capture(monkeypatch, tmp_path):
    body = _raw_body()
    backend, _ = _backend(monkeypatch, body)

    path, _ = backend.capture_image(tmp_path / "p.jpg", _config("RAW"))

    assert path == str(tmp_path / "p.dng")
    assert body.shots[0]["dng"] is True


@pytest.mark.unit
def test_raw_is_stored_and_reported_as_raw_plus_jpeg(monkeypatch, tmp_path):
    backend = _registry_backend(monkeypatch, tmp_path)

    updated = backend.apply_dslr_settings(0, {"image_format": "RAW"})

    assert updated["image_format"] == "RAW+JPEG"
    assert backend.capture_image_format(0) == "RAW+JPEG"
    assert _registry(tmp_path).get_camera_by_id(HW_ID)["image_format"] == "RAW+JPEG"


@pytest.mark.unit
def test_a_stored_raw_is_read_as_raw_plus_jpeg(monkeypatch, tmp_path):
    _store(_registry(tmp_path), image_format="RAW")

    backend = _registry_backend(monkeypatch, tmp_path)

    assert backend.capture_image_format(0) == "RAW+JPEG"


@pytest.mark.unit
def test_the_registry_stores_raw_under_its_current_name(tmp_path):
    registry = _registry(tmp_path)
    registry.register_resolved(HW_ID, {"model": "A2500"}, 0)

    registry.update_image_format(HW_ID, "RAW")

    assert registry.get_camera_by_id(HW_ID)["image_format"] == "RAW+JPEG"


@pytest.mark.unit
def test_the_settings_route_takes_raw_and_reports_raw_plus_jpeg(client, installed):
    backend, _ = installed

    resp = _client_as(client).put("/cameras/dslr/0/settings", json={"image_format": "RAW"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_format"] == "RAW+JPEG"
    assert backend.capture_image_format(0) == "RAW+JPEG"


@pytest.mark.unit
def test_the_settings_route_names_the_two_formats_when_it_refuses_one(client, installed):
    resp = _client_as(client).put("/cameras/dslr/0/settings", json={"image_format": "TIFF"})

    assert resp.status_code == 422
    assert "JPEG, RAW+JPEG" in resp.json()["detail"]
    assert "'TIFF'" in resp.json()["detail"]


# --- the recorded size and EXIF of a DNG master -----------------------------


def _thumbnail_dng(path):
    """A file PIL opens as a 128x96 TIFF, as it opens a CHDK DNG.

    A CHDK DNG's first directory is its 128x96 RGB thumbnail, and PIL reports
    that as the image (checked against an A2500 header with its thumbnail and
    raw data); the raw image sits in a SubIFD PIL does not read.
    """
    from PIL import Image

    Image.new("RGB", (128, 96), "black").save(path, format="TIFF")
    with open(path, "ab") as out:
        out.write(bytes(4096))


@pytest.mark.unit
def test_a_dng_masters_size_and_exif_come_from_its_preview(tmp_path):
    from app.api.cameras import _image_size_and_exif

    master = tmp_path / "p.dng"
    _thumbnail_dng(master)
    preview_sidecar_of(master).write_bytes(_canon_jpeg(1490830))

    width, height, exif = _image_size_and_exif(master)

    assert (width, height) == (16, 16)
    assert exif.get("Model") == "Canon PowerShot A2500"


@pytest.mark.unit
@pytest.mark.parametrize("preview", [None, b"not a jpeg"])
def test_a_dng_master_without_a_readable_preview_records_nothing(tmp_path, preview):
    from app.api.cameras import _image_size_and_exif

    master = tmp_path / "p.dng"
    _thumbnail_dng(master)
    if preview is not None:
        preview_sidecar_of(master).write_bytes(preview)

    assert _image_size_and_exif(master) == (None, None, {})


@pytest.mark.unit
def test_a_jpeg_masters_size_is_read_from_the_master(tmp_path):
    from app.api.cameras import _image_size_and_exif

    master = tmp_path / "p.jpg"
    master.write_bytes(_canon_jpeg(1490830))

    width, height, exif = _image_size_and_exif(master)

    assert (width, height) == (16, 16)
    assert exif.get("Make") == "Canon"


@pytest.mark.unit
def test_a_record_capture_of_a_dng_records_the_previews_size(
    client, db_session, test_project, volume, installed
):
    from app.models.record import RecordImage

    backend, _ = installed
    backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    resp = _client_as(client).post("/cameras/capture", json={
        "project_name": test_project.name, "camera_index": 0,
        "collection_id": volume.id,
    })

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    img = db_session.query(RecordImage).filter(
        RecordImage.id == resp.json()["image_ids"][0]
    ).one()
    assert img.format == "dng"
    assert (img.resolution_width, img.resolution_height) == (16, 16)


@pytest.mark.unit
def test_overlapping_format_requests_leave_cache_and_registry_agreeing(
    monkeypatch, tmp_path
):
    """The first request's registry write is held until the second request has
    finished, or for half a second when the second cannot finish first. The
    last request to write the registry has to be the last to set the cache."""
    import threading

    registry = _registry(tmp_path)
    registry.register_resolved(HW_ID, {"model": "A2500"}, 0)
    second_done = threading.Event()
    first_write = threading.Event()

    class HeldFirstWrite:
        def __init__(self, real):
            self._real = real
            self._calls = 0

        def __getattr__(self, name):
            return getattr(self._real, name)

        def update_image_format(self, hardware_id, image_format):
            self._real.update_image_format(hardware_id, image_format)
            self._calls += 1
            if self._calls == 1:
                first_write.set()
                second_done.wait(0.5)

    held = HeldFirstWrite(registry)
    backend = _registry_backend(monkeypatch, tmp_path)
    backend._registry_factory = lambda: held

    first = threading.Thread(
        target=backend.apply_dslr_settings, args=(0, {"image_format": "RAW+JPEG"})
    )
    first.start()
    assert first_write.wait(5)

    def second_request():
        backend.apply_dslr_settings(0, {"image_format": "JPEG"})
        second_done.set()

    second = threading.Thread(target=second_request)
    second.start()
    first.join(5)
    second.join(5)

    stored = _registry(tmp_path).get_camera_by_id(HW_ID)["image_format"]
    assert backend.capture_image_format(0) == stored
