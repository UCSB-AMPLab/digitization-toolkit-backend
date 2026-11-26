"""Colour card shots in the Fisqua capture manifest (NEH-340, contract 1.12 §13).

- The manifest declares version 1.12; every volume carries
  `calibrationStatus`, and `calibration` lists the shots uploaded and
  verified that apply to at least one page, each with captureId, filename,
  objectKey, sha256, previewObjectKey, previewSha256, rotateDeg, camera,
  capturedAt, settings and appliesFromCaptureId.
- `appliesFromCaptureId` is the first page of the manifest captured after
  the shot's set and before the next set; a shot no page follows stays on
  the unit.
- No shot shares a captureId, objectKey or previewObjectKey with a page or
  another shot, and every name is one path segment of at most 200 bytes:
  otherwise the manifest is not written (rules calibration_identity and
  calibration_key).
- A volume whose shots are not yet verified offsite waits to be sent, as one
  whose pages are not.

No camera and no bucket: the sets, shots and objects are written as the unit
leaves them, on the volumes of tests/unit/test_capture_manifest_export.py.

    python -m pytest tests/unit/test_colour_card_manifest.py
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

import pytest

from tests.unit.test_capture_manifest_export import (  # noqa: F401  (fixtures)
    TENANT, _export, _images, _refusal, tenant, vol,
)
from tests.unit.test_fisqua_outbox import FakeTransport, _delivered, _handover, _rows, _run, fisqua  # noqa: F401


@pytest.fixture
def asked(db_session):
    from app.core import unit_mode
    unit_mode.set_colour_card_shots(db_session, True, actor="admin")


def _last_image_id(db):
    from sqlalchemy import func
    from app.models.record import RecordImage
    return db.query(func.max(RecordImage.id)).scalar() or 0


def card_set(vol, outcome="taken", cameras=(0, 1), raw=False, upload=True, preview_upload=True, first=None,
             **shot_fields):
    """A completed set in `vol`, after every image captured so far, with one shot per camera.

    `first` holds fields given to the first camera's shot only.
    """
    from app.models.colour_card import ColourCardObject, ColourCardSet, ColourCardShot
    db = vol.db
    number = db.query(ColourCardSet).filter(ColourCardSet.collection_id == vol.col.id).count() + 1
    s = ColourCardSet(collection_id=vol.col.id, number=number, outcome=outcome,
                      capture_mode="dual" if len(cameras) == 2 else "single",
                      cameras=[{"camera_index": c, "hardware_id": f"hw{c}", "role": ("left", "right")[c % 2],
                                "settings": {"zoom_step": 3}} for c in cameras],
                      opened_at=datetime(2026, 11, 22, 10, 0), completed_at=datetime(2026, 11, 22, 10, 1),
                      after_image_id=_last_image_id(db))
    db.add(s)
    db.commit()
    shots = []
    if outcome != "taken":
        return s, shots
    folder = vol.root / "calibration"
    folder.mkdir(exist_ok=True)
    for c in cameras:
        ext = "cr2" if raw else "jpg"
        name = f"{vol.stem}-cal_{number:02d}_c{c}.{ext}"
        data = f"{name} bytes".encode()
        (folder / name).write_bytes(data)
        fields = dict(capture_id=str(uuid.uuid4()), filename=name, file_path=str(folder / name),
                      sha256=hashlib.sha256(data).hexdigest(), rotate_deg=90, hardware_id=f"hw{c}",
                      role=("left", "right")[c % 2], captured_at=datetime(2026, 11, 22, 10, 0, 30),
                      settings={"driver": "chdk", "camera": {"zoom_step": 3}, "capture": {}, "exif": {}})
        pdata = None
        if raw:
            pname = f"{vol.stem}-cal_{number:02d}_c{c}_preview.jpg"
            pdata = f"{pname} bytes".encode()
            (folder / pname).write_bytes(pdata)
            fields.update(preview_filename=pname, preview_path=str(folder / pname),
                          preview_sha256=hashlib.sha256(pdata).hexdigest())
        fields.update(shot_fields)
        if first and c == cameras[0]:
            fields.update(first)
        shot = ColourCardShot(set_id=s.id, camera_index=c, **fields)
        db.add(shot)
        db.commit()
        now = datetime.now(timezone.utc)
        if upload:
            db.add(ColourCardObject(shot_id=shot.id, kind="shot",
                                    object_key=f"{TENANT}/{vol.stem}/calibration/{shot.filename}",
                                    local_sha1=hashlib.sha1(data).hexdigest(), uploaded_at=now, verified_at=now))
        if raw and preview_upload:
            db.add(ColourCardObject(shot_id=shot.id, kind="preview",
                                    object_key=f"{TENANT}/{vol.stem}/calibration/{shot.preview_filename}",
                                    local_sha1=hashlib.sha1(pdata).hexdigest(), uploaded_at=now, verified_at=now))
        db.commit()
        shots.append(shot)
    return s, shots


def _doc(client, vol):
    resp = _export(client, vol.col)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.unit
def test_the_manifest_declares_contract_1_12(client, vol):
    from app.core.capture_manifest import VERSION
    vol.record()
    assert VERSION == "1.12" and _doc(client, vol)["version"] == "1.12"


@pytest.mark.unit
def test_with_the_setting_off_a_volume_is_sent_as_skipped_with_no_shots(client, vol):
    card_set(vol)
    vol.record()
    [v] = _doc(client, vol)["volumes"]
    assert v["calibrationStatus"] == "skipped"
    assert "calibration" not in v


@pytest.mark.unit
def test_a_set_taken_before_the_pages_is_present_and_applies_from_the_first_page(client, vol, asked):
    _s, shots = card_set(vol, raw=True)
    vol.record()
    vol.record()
    doc = _doc(client, vol)
    [v] = doc["volumes"]
    assert v["calibrationStatus"] == "present"
    first_page = _images(doc)[0]["captureId"]
    stem = vol.stem
    assert v["calibration"] == [{
        "captureId": shot.capture_id,
        "filename": shot.filename,
        "objectKey": f"{stem}/calibration/{shot.filename}",
        "sha256": shot.sha256,
        "previewObjectKey": f"{stem}/calibration/{shot.filename.rsplit('.', 1)[0]}_preview.jpg",
        "previewSha256": shot.preview_sha256,
        "camera": f"c{shot.camera_index}",
        "capturedAt": "2026-11-22T10:00:30Z",
        "settings": {**shot.settings, "body": {"hardwareId": shot.hardware_id, "role": shot.role,
                                               "cameraIndex": shot.camera_index}},
        "appliesFromCaptureId": first_page,
    } for shot in shots]


@pytest.mark.unit
def test_rotate_deg_follows_the_pages_setting(client, vol, asked, monkeypatch):
    from app.core import config
    card_set(vol)
    vol.record()
    monkeypatch.setattr(config.settings, "FISQUA_ROTATE_DEG", True)
    assert [c["rotateDeg"] for c in _doc(client, vol)["volumes"][0]["calibration"]] == [90, 90]


@pytest.mark.unit
def test_a_later_set_applies_from_the_first_page_after_it_and_one_no_page_follows_is_left_out(client, vol, asked):
    _a, first = card_set(vol)
    vol.record()
    _b, second = card_set(vol)
    vol.record()
    vol.record()
    _c, last = card_set(vol)
    doc = _doc(client, vol)
    pages = [i["captureId"] for i in _images(doc)]
    by_id = {c["captureId"]: c["appliesFromCaptureId"] for c in doc["volumes"][0]["calibration"]}
    assert by_id == {**{s.capture_id: pages[0] for s in first}, **{s.capture_id: pages[2] for s in second}}
    assert not {s.capture_id for s in last} & set(by_id)


@pytest.mark.unit
def test_a_skipped_request_or_pages_before_the_first_set_send_skipped_with_the_sets_it_has(client, vol, asked):
    vol.record()
    _s, shots = card_set(vol)
    vol.record()
    [v] = _doc(client, vol)["volumes"]
    assert v["calibrationStatus"] == "skipped"
    assert [c["captureId"] for c in v["calibration"]] == [s.capture_id for s in shots]


@pytest.mark.unit
def test_a_set_whose_next_set_came_before_any_page_applies_to_none(client, vol, asked):
    _a, first = card_set(vol)
    card_set(vol, outcome="skipped")
    vol.record()
    [v] = _doc(client, vol)["volumes"]
    assert v["calibrationStatus"] == "skipped" and v.get("calibration", []) == []


@pytest.mark.unit
def test_a_shot_not_yet_verified_holds_the_manifest_and_is_never_left_out(client, vol, asked):
    _s, shots = card_set(vol, raw=True, preview_upload=False)
    vol.record()
    problems = _refusal(_export(client, vol.col))
    assert "calibration_not_uploaded" in problems


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    "a/b.jpg", "a\\b.jpg", "a\x07b.jpg", "a\x7fb.jpg", "", ".", "..", "x" * 197 + ".jpg",
])
def test_a_shot_whose_name_is_not_one_short_segment_refuses_the_manifest(client, vol, asked, bad):
    card_set(vol, first={"filename": bad})
    vol.record()
    problems = _refusal(_export(client, vol.col))
    assert "calibration_key" in problems


@pytest.mark.unit
def test_the_name_rule_is_the_contracts():
    from app.core.colour_card import name_problem
    assert name_problem("xx-test-001-cal_01_c0.jpg") is None
    assert name_problem("é" * 100) is None            # 200 bytes
    assert name_problem("é" * 100 + "x") is not None  # 201 bytes
    assert name_problem("\udc80.jpg") is not None     # an unpaired surrogate
    for bad in ("", ".", "..", "a/b", "a\\b", "a\nb", "a\x00b", "a\x7fb"):
        assert name_problem(bad) is not None, bad


@pytest.mark.unit
def test_a_shot_sharing_a_pages_capture_id_refuses_the_manifest(client, vol, asked):
    _s, shots = card_set(vol)
    rec = vol.record()
    shots[0].capture_id = rec.images[0].capture_id
    vol.db.commit()
    assert "calibration_identity" in _refusal(_export(client, vol.col))


@pytest.mark.unit
def test_two_shots_sharing_an_object_key_refuse_the_manifest(client, vol, asked, monkeypatch):
    from app.core import colour_card
    card_set(vol)
    vol.record()
    real = colour_card.evaluate

    def doubled(*args, **kwargs):
        result = real(*args, **kwargs)
        result.entries.append(dict(result.entries[0], captureId=str(uuid.uuid4())))
        return result
    monkeypatch.setattr(colour_card, "evaluate", doubled)
    assert "calibration_identity" in _refusal(_export(client, vol.col))


@pytest.mark.unit
def test_a_shot_preview_key_of_a_page_refuses_the_manifest(client, vol, asked, monkeypatch):
    from app.core import colour_card
    card_set(vol, raw=True)
    vol.record(ext="cr2")
    real = colour_card.evaluate

    def clash(db, collection, tenant, pages):
        result = real(db, collection, tenant, pages)
        result.entries[0]["previewObjectKey"] = pages[0]["previewObjectKey"]
        return result
    monkeypatch.setattr(colour_card, "evaluate", clash)
    assert "calibration_identity" in _refusal(_export(client, vol.col))


# ---------------------------------------------------------------------------
# Sending to Fisqua
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_volume_waits_to_be_sent_until_its_shots_are_verified(client, db_session, vol, fisqua, asked):
    from app.models.colour_card import ColourCardObject
    _s, shots = card_set(vol, upload=False)
    vol.record(status="captured")
    _handover(client, vol.col)
    transport = FakeTransport(sends=[_delivered()])
    _run(db_session, transport)
    assert _rows(db_session, vol.col)[0].state == "waiting_for_upload"
    assert transport.sent == []
    now = datetime.now(timezone.utc)
    for shot in shots:
        db_session.add(ColourCardObject(shot_id=shot.id, kind="shot", object_key=f"{TENANT}/{vol.stem}/calibration/"
                                        f"{shot.filename}", local_sha1="1" * 40, uploaded_at=now, verified_at=now))
    db_session.commit()
    _run(db_session, transport)
    assert _rows(db_session, vol.col)[0].state == "delivered"
    [v] = transport.sent[0]["volumes"]
    assert v["calibrationStatus"] == "present" and len(v["calibration"]) == 2


# ---------------------------------------------------------------------------
# Fisqua's reader of a 1.12 volume's calibration fields
# ---------------------------------------------------------------------------
#
# A Python transcription, not Fisqua's code, of the zod schemas in
# fisqua-dev app/lib/capture/manifest.ts: `calibrationShot` (lines 297-315),
# `calibrationStatusShape` (324-328), the settings cap MAX_SETTINGS_BYTES
# (287), SHA256 (157), MAX_FIELD_CHARS (159) and MAX_KEY_CHARS (160), and the
# rotateDeg check of calibrationProblems (1112).

_FQ_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FQ_MAX_FIELD_CHARS = 256
_FQ_MAX_KEY_CHARS = 1024
_FQ_MAX_SETTINGS_BYTES = 16 * 1024
# z.iso.datetime({offset: true, local: true}): a date, a time, and a Z, an offset or nothing.
_FQ_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_FQ_SHOT_KEYS = {"captureId", "filename", "objectKey", "sha256", "previewObjectKey", "previewSha256",
                 "rotateDeg", "camera", "capturedAt", "settings", "appliesFromCaptureId"}


def _fq_string(value, low, high):
    return isinstance(value, str) and low <= len(value) <= high


def fisqua_calibration_problems(volume):
    """The issues Fisqua's reader reports for a volume's calibration fields; empty when it reads them."""
    out = []
    status = volume.get("calibrationStatus")
    if status is not None and status not in ("present", "skipped"):
        out.append("calibrationStatus")
    if "skippedReason" in volume and volume["skippedReason"] is not None and not isinstance(volume["skippedReason"], str):
        out.append("skippedReason")
    shots = volume.get("calibration")
    if shots is None:
        return out
    if not isinstance(shots, list):
        return out + ["calibration"]
    for i, shot in enumerate(shots):
        at = f"calibration.{i}"
        if not isinstance(shot, dict):
            out.append(at)
            continue
        for key in ("captureId", "filename", "appliesFromCaptureId", "camera"):
            if not _fq_string(shot.get(key), 1, _FQ_MAX_FIELD_CHARS):
                out.append(f"{at}.{key}")
        if not _fq_string(shot.get("objectKey"), 1, _FQ_MAX_KEY_CHARS):
            out.append(f"{at}.objectKey")
        if not (isinstance(shot.get("sha256"), str) and _FQ_SHA256.match(shot["sha256"])):
            out.append(f"{at}.sha256")
        if "previewObjectKey" in shot and not _fq_string(shot["previewObjectKey"], 1, _FQ_MAX_KEY_CHARS):
            out.append(f"{at}.previewObjectKey")
        if "previewSha256" in shot and not (isinstance(shot["previewSha256"], str)
                                            and _FQ_SHA256.match(shot["previewSha256"])):
            out.append(f"{at}.previewSha256")
        if ("previewObjectKey" in shot) != ("previewSha256" in shot):
            out.append(f"{at}.preview")
        captured = shot.get("capturedAt")
        if not (isinstance(captured, str) and len(captured) <= _FQ_MAX_FIELD_CHARS and _FQ_DATETIME.match(captured)):
            out.append(f"{at}.capturedAt")
        settings = shot.get("settings")
        if not isinstance(settings, dict) or not all(isinstance(k, str) for k in settings):
            out.append(f"{at}.settings")
        elif len(json.dumps(settings, separators=(",", ":"), ensure_ascii=False).encode()) > _FQ_MAX_SETTINGS_BYTES:
            out.append(f"{at}.settings.length")
        rotate = shot.get("rotateDeg")
        if rotate is not None and (type(rotate) is not int or rotate not in (0, 90, 180, 270)):
            out.append(f"{at}.rotateDeg")
    return out


# And of calibrationProblems (lines 1044-1121), with calibrationKeyFor (992),
# calibrationPreviewName (997), calibrationPreviewKeyFor (1002), fitsNameBytes
# (1010) and isCalibrationFilename (1020).

def _fq_fits_name_bytes(name):
    try:
        return len(name.encode("utf-8")) <= 200
    except UnicodeEncodeError:  # an unpaired surrogate has no UTF-8 form
        return False


def _fq_is_calibration_filename(name):
    return (name not in ("", ".", "..") and not re.search(r"[\\/\x00-\x1f\x7f]", name)
            and _fq_fits_name_bytes(name))


def _fq_preview_name(filename):
    return re.sub(r"\.[^.]+$", "", filename) + "_preview.jpg"


def fisqua_volume_problems(volume):
    """Field problems (fisqua_calibration_problems) and the rule codes calibrationProblems gives the volume."""
    out = fisqua_calibration_problems(volume)
    if out:
        return out
    shots = volume.get("calibration") or []
    images = [i for r in volume.get("records", []) for i in r.get("images", [])]
    captures = {i["captureId"] for i in images}
    image_keys = {k for i in images for k in (i.get("objectKey"), i.get("previewObjectKey")) if k is not None}
    stem = volume["stem"]
    shot_ids, shot_keys = set(), set()
    for shot in shots:
        name = shot["filename"]
        if not _fq_is_calibration_filename(name) or shot["objectKey"] != f"{stem}/calibration/{name}":
            out.append("calibration-key")
        if "previewObjectKey" in shot:
            if (shot["previewObjectKey"] != f"{stem}/calibration/{_fq_preview_name(name)}"
                    or not _fq_fits_name_bytes(_fq_preview_name(name))):
                out.append("calibration-key")
        if shot["captureId"] in captures or shot["captureId"] in shot_ids:
            out.append("calibration-identity")
        shot_ids.add(shot["captureId"])
        for field in ("objectKey", "previewObjectKey"):
            key = shot.get(field)
            if key is None:
                continue
            if key in image_keys or key in shot_keys:
                out.append("calibration-identity")
            shot_keys.add(key)
        if shot["appliesFromCaptureId"] not in captures:
            out.append("calibration-applies-from")
    return out


@pytest.mark.unit
@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("cameras", [(0,), (0, 1)])
@pytest.mark.parametrize("sets", [1, 2])
def test_every_manifest_captua_sends_passes_fisquas_reader(client, vol, asked, monkeypatch, raw, cameras, sets):
    from app.core import config
    monkeypatch.setattr(config.settings, "FISQUA_ROTATE_DEG", True)
    layout = ("left", "right") if len(cameras) == 2 else ("single",)
    for _ in range(sets):
        card_set(vol, raw=raw, cameras=cameras)
        vol.record(layout=layout, ext="cr2" if raw else "jpg")
    [v] = _doc(client, vol)["volumes"]
    assert len(v["calibration"]) == sets * len(cameras)
    assert fisqua_volume_problems(v) == []
    assert {c["camera"] for c in v["calibration"]} == {f"c{c}" for c in cameras}


def _fq_good_volume():
    page = {"captureId": "page-1", "objectKey": "s/captures/s-img_0001_c0.jpg",
            "previewObjectKey": "s/previews/s-img_0001_c0_preview.jpg"}
    shot = {"captureId": "shot-1", "filename": "s-cal_01_c0.jpg", "objectKey": "s/calibration/s-cal_01_c0.jpg",
            "sha256": "0" * 64, "camera": "c0", "capturedAt": "2026-11-22T10:00:30Z", "settings": {},
            "appliesFromCaptureId": "page-1"}
    return {"stem": "s", "calibrationStatus": "present", "records": [{"images": [page]}], "calibration": [shot]}


@pytest.mark.unit
def test_the_transcription_refuses_what_fisqua_refuses():
    assert fisqua_volume_problems(_fq_good_volume()) == []

    def mutated(**changes):
        v = _fq_good_volume()
        v["calibration"][0].update(changes)
        return fisqua_volume_problems(v)
    for bad in ({"camera": {"cameraIndex": 0}}, {"sha256": "A" * 64}, {"capturedAt": "22/11/2026"},
                {"settings": []}, {"previewObjectKey": "k"}, {"rotateDeg": 45}):
        assert mutated(**bad) != [], bad
    assert mutated(appliesFromCaptureId="another-volumes-page") == ["calibration-applies-from"]
    assert mutated(captureId="page-1") == ["calibration-identity"]
    assert mutated(objectKey="s/captures/s-img_0001_c0.jpg") == ["calibration-key", "calibration-identity"]
    assert mutated(filename="../s-cal_01_c0.jpg", objectKey="s/calibration/../s-cal_01_c0.jpg") == [
        "calibration-key"]
    assert mutated(previewObjectKey="s/previews/s-img_0001_c0_preview.jpg", previewSha256="0" * 64) == [
        "calibration-key", "calibration-identity"]
    twice = _fq_good_volume()
    twice["calibration"].append(dict(twice["calibration"][0]))
    assert fisqua_volume_problems(twice) == ["calibration-identity", "calibration-identity"]
    assert fisqua_calibration_problems({"calibrationStatus": "none"}) == ["calibrationStatus"]


# ---------------------------------------------------------------------------
# What holds a volume back from Fisqua
# ---------------------------------------------------------------------------

def _send(client, db, vol):
    transport = FakeTransport(sends=[_delivered()])
    _run(db, transport)
    return _rows(db, vol.col)[0].state, transport


@pytest.mark.unit
def test_with_the_setting_off_a_pending_shot_holds_nothing(client, db_session, vol, fisqua):
    card_set(vol, upload=False)
    vol.record(status="captured")
    _handover(client, vol.col)
    state, transport = _send(client, db_session, vol)
    assert state == "delivered"
    assert "calibration" not in transport.sent[0]["volumes"][0]


@pytest.mark.unit
def test_turning_the_setting_off_releases_a_volume_held_by_a_pending_shot(client, db_session, vol, fisqua, asked):
    from app.core import unit_mode
    card_set(vol, upload=False)
    vol.record(status="captured")
    _handover(client, vol.col)
    assert _send(client, db_session, vol)[0] == "waiting_for_upload"
    unit_mode.set_colour_card_shots(db_session, False, actor="admin")
    state, transport = _send(client, db_session, vol)
    assert state == "delivered"
    assert transport.sent[0]["volumes"][0]["calibrationStatus"] == "skipped"


@pytest.mark.unit
def test_a_shot_no_page_follows_holds_nothing(client, db_session, vol, fisqua, asked):
    card_set(vol)
    vol.record(status="captured")
    card_set(vol, upload=False)
    _handover(client, vol.col)
    state, transport = _send(client, db_session, vol)
    assert state == "delivered"
    assert len(transport.sent[0]["volumes"][0]["calibration"]) == 2


@pytest.mark.unit
@pytest.mark.parametrize("how", ["master_gone", "preview_gone", "fixity", "remote_differs"])
def test_a_shot_that_cannot_be_delivered_holds_nothing_and_makes_the_volume_skipped(
        client, db_session, vol, fisqua, asked, how):
    from app.models.colour_card import ColourCardObject
    _s, shots = card_set(vol, raw=True, upload=False, preview_upload=False)
    _b, others = card_set(vol, cameras=(0,), raw=False)  # a later set that is fine: it applies to the page
    vol.record(status="captured")
    shot = shots[0]
    if how == "master_gone":
        (vol.root / "calibration" / shot.filename).unlink()
    elif how == "preview_gone":
        (vol.root / "calibration" / shot.preview_filename).unlink()
    else:
        db_session.add(ColourCardObject(shot_id=shot.id, kind="shot", object_key=f"{TENANT}/{vol.stem}/calibration/"
                                        f"{shot.filename}", local_sha1="1" * 40,
                                        refused_at=datetime.now(timezone.utc), refusal=how))
        db_session.commit()
    _handover(client, vol.col)
    state, transport = _send(client, db_session, vol)
    assert state == "delivered"
    [v] = transport.sent[0]["volumes"]
    assert v["calibrationStatus"] == "skipped"
    assert shot.capture_id not in {c["captureId"] for c in v.get("calibration", [])}


# ---------------------------------------------------------------------------
# Keeping a volume on the unit
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("state", ["verified", "uploaded", "pending"])
def test_keeping_a_volume_whose_shot_is_offsite_is_a_409(client, db_session, vol, asked, state):
    from tests.unit.test_capture_manifest_export import _as
    from app.models.colour_card import ColourCardObject
    _s, shots = card_set(vol, cameras=(0,), upload=False)
    now = datetime.now(timezone.utc)
    db_session.add(ColourCardObject(shot_id=shots[0].id, kind="shot", local_sha1="1" * 40,
                                    object_key=f"{TENANT}/{vol.stem}/calibration/{shots[0].filename}",
                                    uploaded_at=now if state != "pending" else None,
                                    verified_at=now if state == "verified" else None))
    db_session.commit()
    resp = _as(client, "admin").post(f"/collections/{vol.col.id}/keep-on-unit")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "already_left_unit"


@pytest.mark.unit
def test_a_refused_shot_object_does_not_stop_a_keep(client, db_session, vol, asked):
    from tests.unit.test_capture_manifest_export import _as
    from app.models.colour_card import ColourCardObject
    _s, shots = card_set(vol, cameras=(0,), upload=False)
    db_session.add(ColourCardObject(shot_id=shots[0].id, kind="shot", local_sha1="1" * 40,
                                    object_key=f"{TENANT}/{vol.stem}/calibration/{shots[0].filename}",
                                    refused_at=datetime.now(timezone.utc), refusal="fixity"))
    db_session.commit()
    assert _as(client, "admin").post(f"/collections/{vol.col.id}/keep-on-unit").status_code == 200


@pytest.mark.unit
def test_a_kept_volumes_files_count_its_taken_shots(client, db_session, vol, asked):
    from app.core.kept_volumes import kept_volumes
    vol.record(upload=False)
    card_set(vol, raw=True, upload=False, preview_upload=False)
    card_set(vol, outcome="skipped")
    vol.col.kept_on_unit = True
    db_session.commit()
    [kept] = kept_volumes(db_session)
    assert kept["files"] == 2 + 4  # two pages, two shots and their previews


@pytest.mark.unit
def test_the_setting_is_read_once_for_the_gate_and_the_manifest(client, db_session, vol, fisqua, monkeypatch):
    """The reviewer's sequence: the setting turned on between the outbox's check and its build."""
    from app.core import colour_card
    card_set(vol, upload=False)
    vol.record(status="captured")
    _handover(client, vol.col)
    reads = []

    def setting(db):
        reads.append(1)
        return len(reads) > 1  # off for the first read, on for every later one
    monkeypatch.setattr(colour_card, "setting_on", setting)
    state, transport = _send(client, db_session, vol)
    if state == "delivered":
        [v] = transport.sent[0]["volumes"]
        assert not (v["calibrationStatus"] == "present" and not v.get("calibration")), \
            "a manifest that says present carries its shots"
    else:
        assert state == "waiting_for_upload"


@pytest.mark.unit
def test_a_deliverable_shot_not_yet_verified_keeps_the_volume_waiting(client, db_session, vol, fisqua, asked):
    card_set(vol, upload=False)
    vol.record(status="captured")
    _handover(client, vol.col)
    assert _send(client, db_session, vol)[0] == "waiting_for_upload"
    assert _send(client, db_session, vol)[0] == "waiting_for_upload"


# ---------------------------------------------------------------------------
# What GET .../colour-card says of each taken set's delivery
# ---------------------------------------------------------------------------

def _delivery(client, vol):
    from tests.unit.test_capture_manifest_export import _as
    resp = _as(client, "operator").get(f"/collections/{vol.col.id}/colour-card")
    assert resp.status_code == 200, resp.text
    return [(s["outcome"], s["delivery"], s["delivery_reason"]) for s in resp.json()["sets"]]


@pytest.mark.unit
def test_each_taken_set_says_whether_it_was_sent_waits_or_cannot_be_delivered(client, db_session, vol, asked):
    from app.models.colour_card import ColourCardObject
    card_set(vol)
    _b, waiting = card_set(vol, upload=False)
    _c, gone = card_set(vol, upload=False)
    _d, refused = card_set(vol, upload=False)
    card_set(vol, outcome="skipped")
    vol.record()
    (vol.root / "calibration" / gone[1].filename).unlink()
    db_session.add(ColourCardObject(shot_id=refused[0].id, kind="shot", local_sha1="1" * 40,
                                    object_key=f"{TENANT}/{vol.stem}/calibration/{refused[0].filename}",
                                    refused_at=datetime.now(timezone.utc), refusal="fixity"))
    db_session.commit()
    assert _delivery(client, vol) == [
        ("taken", "sent", None),
        ("taken", "waiting", None),
        ("taken", "undeliverable", "file_missing"),
        ("taken", "undeliverable", "upload_refused"),
        ("skipped", None, None),
    ]
