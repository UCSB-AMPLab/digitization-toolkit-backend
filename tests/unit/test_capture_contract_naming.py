#!/usr/bin/env python3
"""New captures are named as exchange contract 1.1, section 3 says.

Each image of a capture event is `<filename_stem>-img_<nnnn>_c<n>.<ext>`:
<nnnn> is the number next_capture_number hands out for the event, zero-padded
to four digits, and `_c<n>` is the camera index, appended on every capture.
Both images of a dual capture carry one number, and `<stem>-img_<nnnn>` is
their pair id. A RAW master's preview is `<stem>-img_<nnnn>_c<n>_preview.jpg`
in the volume's `previews/`. A retake takes the next number and never the
superseded image's. Nothing already on disk is renamed or overwritten.

These go through the capture endpoints and the real capture service, with
only the camera stood in for: the fake backend writes a master where the
service asks (a `.cr2` and its sidecar for a RAW rig, as the gphoto2 backend
does).

Run with: python -m pytest tests/unit/test_capture_contract_naming.py
"""

import json
from pathlib import Path

import pytest

import capture.project_manager as project_manager_module
import capture.service as capture_service


STEM = "co-test-naming"


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username="op", role="operator"):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


class _FakeBackend:
    """Writes a master where capture asks; for RAW, a `.cr2` and its sidecar where preview_sidecar_of names it."""

    def __init__(self, raw=False):
        self.raw = raw
        self.requested = []

    def capture_image(self, output_path, camera_config, capture_output=False):
        from app.core.paths import preview_sidecar_of

        out = Path(output_path)
        self.requested.append(out.name)
        if self.raw:
            out = out.with_suffix(".cr2")
            out.write_bytes(b"raw bytes of " + out.name.encode())
            preview_sidecar_of(out).write_bytes(b"preview of " + out.name.encode())
        else:
            out.write_bytes(b"jpeg bytes of " + out.name.encode())
        return out, None


@pytest.fixture
def backend(monkeypatch):
    b = _FakeBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: b)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(
        project_manager_module, "default_camera_config_from_registry",
        lambda camera_index, resolution="high", registry=None: (
            {"camera_index": camera_index, "rotate_deg": 0}, "hw"))
    return b


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection
    v = Collection(name="Tomo 55", project_id=test_project.id, filename_stem=STEM)
    db_session.add(v)
    db_session.commit()
    return v


def _single(api, project, volume, **extra):
    payload = {"project_name": project.name, "camera_index": 0, "collection_id": volume.id}
    payload.update(extra)
    resp = api.post("/cameras/capture", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"], resp.json()
    return resp.json()


def _dual(api, project, volume, **extra):
    payload = {"project_name": project.name, "collection_id": volume.id}
    payload.update(extra)
    resp = api.post("/cameras/capture/dual", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"], resp.json()
    return resp.json()


def _counter(db_session, volume):
    from app.models.collection import Collection
    db_session.expire_all()
    return db_session.get(Collection, volume.id).capture_counter


def _images(db_session, record_id):
    from app.models.record import RecordImage
    db_session.expire_all()
    return (db_session.query(RecordImage).filter(RecordImage.record_id == record_id)
            .order_by(RecordImage.id).all())


def _captures(root):
    return sorted(p.name for p in (root / STEM / "captures").iterdir())


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("number,expected", [
    (1, f"{STEM}-img_0001"),
    (42, f"{STEM}-img_0042"),
    (9999, f"{STEM}-img_9999"),
    (10000, f"{STEM}-img_10000"),
])
def test_the_filename_index_is_zero_padded_to_four_digits_and_never_truncated(number, expected):
    assert capture_service.capture_filename_index(STEM, number) == expected


@pytest.mark.unit
@pytest.mark.parametrize("stem,number", [(None, 1), ("", 1), (STEM, 0), (STEM, -1), (STEM, "1")])
def test_the_filename_index_refuses_a_missing_stem_or_a_non_number(stem, number):
    with pytest.raises(ValueError):
        capture_service.capture_filename_index(stem, number)


@pytest.mark.unit
def test_there_is_no_timestamp_fallback():
    with pytest.raises(ValueError):
        capture_service.image_filename(camera_index=0, index=None)
    with pytest.raises(ValueError):
        capture_service.single_capture_image("P", object(), check_camera=False, stem=STEM)
    with pytest.raises(ValueError):
        capture_service.dual_capture_image("P", object(), object(), check_camera=False, stem=STEM)


@pytest.mark.unit
def test_a_name_is_taken_under_any_suffix_case_or_as_a_sidecar(tmp_path):
    captures = tmp_path / STEM / "captures"
    previews = tmp_path / STEM / "previews"
    captures.mkdir(parents=True)
    previews.mkdir()
    taken = capture_service.capture_name_taken

    assert not taken(captures, f"{STEM}-img_0001")
    assert not taken(tmp_path / "missing" / "captures", f"{STEM}-img_0001")

    (captures / f"{STEM.upper()}-IMG_0001_C1.CR2").write_bytes(b"x")
    assert taken(captures, f"{STEM}-img_0001"), "either camera of the event, in any case"
    assert taken(captures, f"{STEM}-img_0001_c1")
    assert not taken(captures, f"{STEM}-img_0001_c0")
    assert not taken(captures, f"{STEM}-img_000"), "a shorter number is another name"

    (previews / f"{STEM}-img_0002_c0_preview.jpg").write_bytes(b"x")
    assert taken(captures, f"{STEM}-img_0002")
    assert taken(captures, f"{STEM}-img_0002_c0")
    assert not taken(captures, f"{STEM}-img_00021")


# ---------------------------------------------------------------------------
# Single and dual captures
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_single_captures_are_named_by_the_counter_and_the_c_suffix(
        client, db_session, test_project, volume, backend, override_projects_root):
    api = _client_as(client)

    first = _single(api, test_project, volume)
    assert _counter(db_session, volume) == 1
    second = _single(api, test_project, volume, camera_index=1)
    assert _counter(db_session, volume) == 2

    assert Path(first["file_path"]) == override_projects_root / STEM / "captures" / f"{STEM}-img_0001_c0.jpg"
    assert Path(second["file_path"]).name == f"{STEM}-img_0002_c1.jpg"
    assert _captures(override_projects_root) == [f"{STEM}-img_0001_c0.jpg", f"{STEM}-img_0002_c1.jpg"]

    img = _images(db_session, first["record_id"])[0]
    assert img.filename == f"{STEM}-img_0001_c0.jpg"
    assert img.pair_id is None, "a single capture has no pair"


@pytest.mark.unit
def test_a_dual_capture_shares_one_number_and_differs_in_the_c_suffix(
        client, db_session, test_project, volume, backend, override_projects_root):
    api = _client_as(client)
    volume.capture_counter = 6
    db_session.commit()

    body = _dual(api, test_project, volume)

    assert _counter(db_session, volume) == 7, "one number for the pair"
    assert [Path(p).name for p in body["file_paths"]] == [
        f"{STEM}-img_0007_c0.jpg", f"{STEM}-img_0007_c1.jpg"]
    imgs = _images(db_session, body["record_id"])
    assert {i.pair_id for i in imgs} == {f"{STEM}-img_0007"}
    assert {i.role for i in imgs} == {"left", "right"}


@pytest.mark.unit
def test_a_27_character_stem_makes_a_36_character_pair_id(
        client, db_session, test_project, backend, override_projects_root):
    from app.models.collection import Collection, FILENAME_STEM_MAX_LENGTH, PAIR_ID_MAX_LENGTH
    from app.models.record import RecordImage

    stem = "co-ahr-con-caj001-car001-xy"
    assert len(stem) == FILENAME_STEM_MAX_LENGTH == 27
    assert RecordImage.__table__.columns["pair_id"].type.length == PAIR_ID_MAX_LENGTH == 36
    v = Collection(name="Long", project_id=test_project.id, filename_stem=stem)
    db_session.add(v)
    db_session.commit()

    body = _dual(_client_as(client), test_project, v)

    pair_ids = {i.pair_id for i in _images(db_session, body["record_id"])}
    assert pair_ids == {f"{stem}-img_0001"}
    assert len(pair_ids.pop()) == 36


@pytest.mark.unit
def test_a_raw_masters_preview_is_named_from_it_in_the_volumes_previews(
        client, db_session, test_project, volume, backend, override_projects_root):
    backend.raw = True
    api = _client_as(client)

    body = _dual(api, test_project, volume)

    vol = override_projects_root / STEM
    assert [Path(p) for p in body["file_paths"]] == [
        vol / "captures" / f"{STEM}-img_0001_c0.cr2", vol / "captures" / f"{STEM}-img_0001_c1.cr2"]
    assert sorted(p.name for p in (vol / "previews").iterdir()) == [
        f"{STEM}-img_0001_c0_preview.jpg", f"{STEM}-img_0001_c1_preview.jpg"]
    assert not any(p.name.endswith("_preview.jpg") for p in (vol / "captures").iterdir())


@pytest.mark.unit
def test_the_manifest_line_written_at_capture_carries_the_new_filenames(
        client, db_session, test_project, volume, backend, override_projects_root):
    backend.raw = True
    api = _client_as(client)

    single = _single(api, test_project, volume)
    dual = _dual(api, test_project, volume)

    lines = [json.loads(l) for l in
             (override_projects_root / STEM / "metadata" / "manifest.jsonl").read_text().splitlines()]
    assert len(lines) == 2
    assert [f["relative_path"] for f in lines[0]["files"]] == [f"captures/{STEM}-img_0001_c0.cr2"]
    assert lines[0]["pair_id"] is None
    assert sorted(f["relative_path"] for f in lines[1]["files"]) == [
        f"captures/{STEM}-img_0002_c0.cr2", f"captures/{STEM}-img_0002_c1.cr2"]
    assert lines[1]["pair_id"] == f"{STEM}-img_0002"
    # The rows point at the same capture events.
    assert _images(db_session, single["record_id"])[0].capture_id == lines[0]["capture_id"]
    assert {i.capture_id for i in _images(db_session, dual["record_id"])} == {lines[1]["capture_id"]}


# ---------------------------------------------------------------------------
# Retakes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_retake_takes_the_next_number_and_the_counter_never_goes_back(
        client, db_session, test_project, volume, backend, override_projects_root):
    api = _client_as(client)

    first = _single(api, test_project, volume)
    _single(api, test_project, volume)
    assert _counter(db_session, volume) == 2

    retake = _single(api, test_project, volume, record_id=first["record_id"])

    assert retake["record_id"] == first["record_id"]
    assert _counter(db_session, volume) == 3
    imgs = _images(db_session, first["record_id"])
    assert [(i.filename, i.is_current) for i in imgs] == [
        (f"{STEM}-img_0001_c0.jpg", False), (f"{STEM}-img_0003_c0.jpg", True)]
    # The superseded image keeps its file and its name.
    assert (override_projects_root / STEM / "captures" / f"{STEM}-img_0001_c0.jpg").read_bytes() == (
        f"jpeg bytes of {STEM}-img_0001_c0.jpg".encode())


@pytest.mark.unit
def test_a_dual_retake_after_a_supersede_takes_the_next_number(
        client, db_session, test_project, volume, backend, override_projects_root):
    api = _client_as(client)

    first = _dual(api, test_project, volume)
    _dual(api, test_project, volume, record_id=first["record_id"])
    _dual(api, test_project, volume, record_id=first["record_id"])

    assert _counter(db_session, volume) == 3
    current = [i.filename for i in _images(db_session, first["record_id"]) if i.is_current]
    assert sorted(current) == [f"{STEM}-img_0003_c0.jpg", f"{STEM}-img_0003_c1.jpg"]
    assert len(_captures(override_projects_root)) == 6, "no file was overwritten"


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["single", "dual"])
def test_a_retake_of_a_rejected_record_ends_captured_and_its_history_row_says_so(
        client, db_session, test_project, volume, backend, mode):
    from app.models.record import Record, RecordStatusChange

    api = _client_as(client)
    take = _single if mode == "single" else _dual
    first = take(api, test_project, volume)
    rec = db_session.get(Record, first["record_id"])
    rec.status = "rejected"
    db_session.commit()

    take(api, test_project, volume, record_id=rec.id)

    db_session.expire_all()
    assert db_session.get(Record, rec.id).status == "captured"
    rows = [(c.from_status, c.to_status, c.changed_by, c.collection_id)
            for c in db_session.query(RecordStatusChange).filter(RecordStatusChange.record_id == rec.id)]
    assert rows == [("rejected", "captured", "op", volume.id)]


# ---------------------------------------------------------------------------
# Files already on disk
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_number_a_file_on_disk_already_carries_is_passed_over(
        client, db_session, test_project, volume, backend, override_projects_root):
    """The counter behind the disk: a catalogue restored from before captures still on the card."""
    vol = override_projects_root / STEM
    (vol / "captures").mkdir(parents=True)
    (vol / "previews").mkdir()
    (vol / "captures" / f"{STEM}-img_0001_c0.cr2").write_bytes(b"older master")
    (vol / "previews" / f"{STEM}-img_0002_c1_preview.jpg").write_bytes(b"older preview")

    body = _single(_client_as(client), test_project, volume)

    assert Path(body["file_path"]).name == f"{STEM}-img_0003_c0.jpg"
    assert _counter(db_session, volume) == 3
    assert (vol / "captures" / f"{STEM}-img_0001_c0.cr2").read_bytes() == b"older master"
    assert (vol / "previews" / f"{STEM}-img_0002_c1_preview.jpg").read_bytes() == b"older preview"


@pytest.mark.unit
def test_existing_files_keep_their_names(
        client, db_session, test_project, volume, backend, override_projects_root):
    """A capture renames nothing: an image named before contract naming stays as it is."""
    from app.models.record import Record, RecordImage

    captures = override_projects_root / STEM / "captures"
    captures.mkdir(parents=True)
    old = captures / "20260104_035632_123_c0.jpg"
    old.write_bytes(b"old")
    rec = Record(title="old", collection_id=volume.id, capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=old.name, file_path=str(old),
                               format="jpg", role="single"))
    volume.capture_counter = 1
    db_session.commit()

    _single(_client_as(client), test_project, volume, record_id=rec.id)

    assert _captures(override_projects_root) == ["20260104_035632_123_c0.jpg", f"{STEM}-img_0002_c0.jpg"]
    assert [i.filename for i in _images(db_session, rec.id)] == [old.name, f"{STEM}-img_0002_c0.jpg"]


@pytest.mark.unit
def test_capture_image_refuses_a_name_taken_after_the_check(tmp_path, monkeypatch):
    """The last guard: a master or sidecar already carrying the name, under any suffix, is never overwritten."""
    from capture.camera import CameraConfig

    monkeypatch.setattr(capture_service, "get_backend", lambda: _FakeBackend(raw=True))
    captures = tmp_path / STEM / "captures"
    captures.mkdir(parents=True)
    (captures / f"{STEM}-img_0001_c0.cr2").write_bytes(b"older master")

    with pytest.raises(FileExistsError):
        capture_service.capture_image(
            "P", CameraConfig(camera_index=0), output_filename=f"{STEM}-img_0001_c0.jpg",
            check_camera=False, stem=STEM, root=tmp_path)
    assert (captures / f"{STEM}-img_0001_c0.cr2").read_bytes() == b"older master"


# ---------------------------------------------------------------------------
# A name longer than a pair id holds
# ---------------------------------------------------------------------------

STEM_27 = "co-ahr-con-caj001-car001-xy"
STEM_26 = "co-ahr-con-caj001-car001-x"


def _volume_at(db_session, project, stem, counter):
    from app.models.collection import Collection
    v = Collection(name=stem, project_id=project.id, filename_stem=stem, capture_counter=counter)
    db_session.add(v)
    db_session.commit()
    return v


def _post(api, project, volume, dual=False, **extra):
    payload = {"project_name": project.name, "collection_id": volume.id}
    if not dual:
        payload["camera_index"] = 0
    payload.update(extra)
    return api.post("/cameras/capture/dual" if dual else "/cameras/capture", json=payload)


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True])
def test_a_name_longer_than_a_pair_id_is_refused_before_the_shutter_fires(
        client, db_session, test_project, backend, override_projects_root, dual):
    from app.core.capture_naming import CAPTURE_NAME_TOO_LONG_DETAIL

    v = _volume_at(db_session, test_project, STEM_27, 9999)

    resp = _post(_client_as(client), test_project, v, dual=dual)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == CAPTURE_NAME_TOO_LONG_DETAIL
    assert backend.requested == [], "no camera fired"
    assert _counter(db_session, v) == 9999, "the refused capture consumed no number"
    assert not (override_projects_root / STEM_27 / "captures").exists() or not any(
        (override_projects_root / STEM_27 / "captures").iterdir())


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True])
def test_a_retake_whose_name_is_too_long_is_refused_and_the_record_is_left_as_it_was(
        client, db_session, test_project, backend, override_projects_root, dual):
    from app.core.capture_naming import CAPTURE_NAME_TOO_LONG_DETAIL
    from app.models.record import Record, RecordImage

    v = _volume_at(db_session, test_project, STEM_27, 9999)
    rec = Record(title="r", collection_id=v.id, status="rejected", capture_mode="dual" if dual else "single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename="old.jpg", file_path="/old.jpg",
                               format="jpg", role="single", is_current=True))
    db_session.commit()

    resp = _post(_client_as(client), test_project, v, dual=dual, record_id=rec.id)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == CAPTURE_NAME_TOO_LONG_DETAIL
    assert backend.requested == []
    db_session.expire_all()
    assert db_session.get(Record, rec.id).status == "rejected"
    assert [i.is_current for i in _images(db_session, rec.id)] == [True]
    assert _counter(db_session, v) == 9999


@pytest.mark.unit
def test_a_27_character_stem_still_captures_number_9999(
        client, db_session, test_project, backend, override_projects_root):
    v = _volume_at(db_session, test_project, STEM_27, 9998)

    body = _dual(_client_as(client), test_project, v)

    assert [Path(p).name for p in body["file_paths"]] == [
        f"{STEM_27}-img_9999_c0.jpg", f"{STEM_27}-img_9999_c1.jpg"]


@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True])
def test_a_shorter_stem_is_not_refused_for_a_five_digit_number(
        client, db_session, test_project, backend, override_projects_root, dual):
    """Whether five digits are acceptable is not settled; only the 36-character limit is enforced."""
    v = _volume_at(db_session, test_project, STEM_26, 9999)

    body = (_dual if dual else _single)(_client_as(client), test_project, v)

    names = [Path(p).name for p in body["file_paths"]] if dual else [Path(body["file_path"]).name]
    assert all(n.startswith(f"{STEM_26}-img_10000_c") for n in names)
    assert _counter(db_session, v) == 10000


# ---------------------------------------------------------------------------
# Numbers passed over, with a cap
# ---------------------------------------------------------------------------

def _occupy(root, numbers):
    captures = root / STEM / "captures"
    captures.mkdir(parents=True, exist_ok=True)
    for n in numbers:
        (captures / f"{STEM}-img_{n:04d}_c0.cr2").write_bytes(b"older")


@pytest.mark.unit
def test_up_to_the_cap_numbers_on_disk_are_passed_over(
        client, db_session, test_project, volume, backend, override_projects_root, monkeypatch):
    from app.core import capture_naming
    monkeypatch.setattr(capture_naming, "MAX_NUMBERS_PASSED_OVER", 3)
    _occupy(override_projects_root, [1, 2, 3])

    body = _single(_client_as(client), test_project, volume)

    assert Path(body["file_path"]).name == f"{STEM}-img_0004_c0.jpg"


@pytest.mark.unit
def test_more_numbers_on_disk_than_the_cap_refuse_the_capture(
        client, db_session, test_project, volume, backend, override_projects_root, monkeypatch):
    from app.core import capture_naming
    monkeypatch.setattr(capture_naming, "MAX_NUMBERS_PASSED_OVER", 3)
    _occupy(override_projects_root, [1, 2, 3, 4])

    resp = _post(_client_as(client), test_project, volume)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == capture_naming.CAPTURE_NUMBERS_TAKEN_DETAIL
    assert backend.requested == []
    assert _counter(db_session, volume) == 0


@pytest.mark.unit
def test_the_default_cap_is_a_hundred():
    from app.core.capture_naming import MAX_NUMBERS_PASSED_OVER
    assert MAX_NUMBERS_PASSED_OVER == 100


# ---------------------------------------------------------------------------
# No resolution in the name
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("dual", [False, True])
def test_a_resolution_in_the_file_name_is_refused(
        client, db_session, test_project, volume, backend, dual):
    from app.core.capture_naming import RESOLUTION_IN_FILENAME_DETAIL

    resp = _post(_client_as(client), test_project, volume, dual=dual, include_resolution_in_filename=True)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == RESOLUTION_IN_FILENAME_DETAIL
    assert backend.requested == []
    assert _counter(db_session, volume) == 0


@pytest.mark.unit
def test_no_resolution_in_the_file_name_is_accepted(
        client, db_session, test_project, volume, backend):
    body = _single(_client_as(client), test_project, volume, include_resolution_in_filename=False)
    assert Path(body["file_path"]).name == f"{STEM}-img_0001_c0.jpg"


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

@pytest.fixture
def upload_data(monkeypatch, tmp_path):
    import app.api.records as records_api
    monkeypatch.setattr(records_api.settings, "DTK_DATA_DIR", str(tmp_path / "data"))


def _upload(api, record_id, name="page.jpg", content_type="image/jpeg"):
    return api.post(f"/records/{record_id}/images", files={"file": (name, b"not really an image", content_type)})


def _record_in(db_session, volume):
    from app.models.record import Record
    rec = Record(title="r", collection_id=volume.id, capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    return rec


@pytest.mark.unit
def test_an_upload_is_named_as_a_single_capture_with_its_number(
        client, db_session, test_project, volume, upload_data, override_projects_root):
    rec = _record_in(db_session, volume)
    volume.capture_counter = 4
    db_session.commit()

    resp = _upload(_client_as(client), rec.id, name="Scan 12.JPG")

    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == f"{STEM}-img_0005_c0.jpg"
    assert Path(resp.json()["file_path"]) == override_projects_root / STEM / "captures" / f"{STEM}-img_0005_c0.jpg"
    assert _counter(db_session, volume) == 5


@pytest.mark.unit
@pytest.mark.parametrize("name,content_type,suffix", [
    ("page.TIFF", "image/tiff", ".tiff"),
    ("page", "image/png", ".png"),
    ("page.exe", "image/webp", ".webp"),
])
def test_an_upload_keeps_a_known_suffix_and_otherwise_takes_its_types(
        client, db_session, test_project, volume, upload_data, name, content_type, suffix):
    rec = _record_in(db_session, volume)

    resp = _upload(_client_as(client), rec.id, name=name, content_type=content_type)

    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == f"{STEM}-img_0001_c0{suffix}"


@pytest.mark.unit
def test_an_upload_passes_over_a_number_on_disk(
        client, db_session, test_project, volume, upload_data, override_projects_root):
    rec = _record_in(db_session, volume)
    _occupy(override_projects_root, [1])

    resp = _upload(_client_as(client), rec.id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["filename"] == f"{STEM}-img_0002_c0.jpg"
    assert (override_projects_root / STEM / "captures" / f"{STEM}-img_0001_c0.cr2").read_bytes() == b"older"


@pytest.mark.unit
def test_an_upload_whose_name_is_too_long_is_refused_and_writes_nothing(
        client, db_session, test_project, upload_data, override_projects_root):
    from app.core.capture_naming import CAPTURE_NAME_TOO_LONG_DETAIL

    v = _volume_at(db_session, test_project, STEM_27, 9999)
    rec = _record_in(db_session, v)

    resp = _upload(_client_as(client), rec.id)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == CAPTURE_NAME_TOO_LONG_DETAIL
    assert _counter(db_session, v) == 9999
    assert _images(db_session, rec.id) == []
    assert not any((override_projects_root / STEM_27 / "captures").iterdir())


# ---------------------------------------------------------------------------
# Numbers the manifest names
# ---------------------------------------------------------------------------

def _manifest_names(root):
    m = root / STEM / "metadata" / "manifest.jsonl"
    if not m.exists():
        return []
    names = []
    for line in m.read_text().splitlines():
        if line.strip():
            names += [Path(f["relative_path"]).name for f in json.loads(line)["files"]]
    return names


@pytest.mark.unit
def test_a_restored_catalogue_does_not_reuse_a_number_whose_file_was_deleted(
        client, db_session, test_project, volume, backend, override_projects_root):
    """The counter goes back to a backup's value; image 2 was deleted from the card; 3 is still there."""
    from sqlalchemy import text
    from app.models.record import RecordImage

    api = _client_as(client, role="admin")
    for _ in range(3):
        _single(api, test_project, volume)
    img2 = db_session.query(RecordImage).filter(RecordImage.filename == f"{STEM}-img_0002_c0.jpg").one()
    assert api.delete(f"/records/images/{img2.id}").status_code == 200
    assert f"{STEM}-img_0002_c0.jpg" not in _captures(override_projects_root)
    db_session.execute(text("UPDATE collections SET capture_counter = 1 WHERE id = :i"), {"i": volume.id})
    db_session.commit()

    body = _single(api, test_project, volume)

    assert Path(body["file_path"]).name == f"{STEM}-img_0004_c0.jpg"
    names = _manifest_names(override_projects_root)
    assert len(names) == len(set(names)), "no name is given twice"


@pytest.mark.unit
def test_the_manifest_is_read_incrementally_and_a_torn_last_line_waits(tmp_path):
    from app.core.capture_naming import manifest_capture_numbers

    m = tmp_path / "manifest.jsonl"
    assert manifest_capture_numbers(m, STEM) == set()
    m.write_text(f'{{"files": [{{"relative_path": "captures/{STEM}-img_0001_c0.cr2"}}]}}\n')
    assert manifest_capture_numbers(m, STEM) == {1}
    with open(m, "a") as f:
        f.write(f'{{"files": [{{"relative_path": "captures/{STEM.upper()}-IMG_0007_C1.jpg"}}]}}\n'
                f'{{"files": [{{"relative_path": "captures/{STEM}-img_0009_c0')
    assert manifest_capture_numbers(m, STEM) == {1, 7}, "the unfinished line is not read yet"
    with open(m, "a") as f:
        f.write('.jpg"}]}\n')
    assert manifest_capture_numbers(m, STEM) == {1, 7, 9}
    assert manifest_capture_numbers(m, "co-other") == set()
    m.write_text("")
    assert manifest_capture_numbers(m, STEM) == set(), "a replaced, shorter file is read again"
