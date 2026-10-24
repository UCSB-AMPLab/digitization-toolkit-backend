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
def test_single_captures_are_named_by_the_counter_with_the_camera_index(
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
def test_a_dual_capture_shares_one_number_and_differs_in_the_camera_index(
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
