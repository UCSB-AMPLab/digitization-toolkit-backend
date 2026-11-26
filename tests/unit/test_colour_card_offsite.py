"""Colour card shots offsite (NEH-340): uploaded beside the pages, never freed.

Each shot of a taken set, and a RAW shot's preview, goes to
`<tenant>/<stem>/calibration/<file name>` with the checks a page's upload
makes (the listing before and after, the bytes staged, the shot's own
sha256), recorded in colour_card_objects. Freeing a volume's space frees no
shot, and nothing in the list of volumes to free counts one.

Against a local directory remote behind rclone, as tests/unit/test_image_upload.py.

    python -m pytest tests/unit/test_colour_card_offsite.py
"""

import hashlib
import uuid
from datetime import datetime

import pytest

from tests.unit.test_image_upload import STEM, TENANT, _image, _run, _sha1, _volume, remote  # noqa: F401


def _shots(db, vol, root, outcome="taken", raw=False, number=None):
    """A completed set in `vol` with a shot from cameras 0 and 1, files on disk."""
    from app.models.colour_card import ColourCardSet, ColourCardShot

    number = number or db.query(ColourCardSet).filter(ColourCardSet.collection_id == vol.id).count() + 1
    s = ColourCardSet(collection_id=vol.id, number=number, outcome=outcome, capture_mode="dual", cameras=[],
                      opened_at=datetime(2026, 11, 22), completed_at=None if outcome is None else datetime(2026, 11, 22),
                      after_image_id=0)
    db.add(s)
    db.commit()
    folder = root / vol.filename_stem / "calibration"
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for cam in (0, 1):
        name = f"{vol.filename_stem}-cal_{number:02d}_c{cam}.{'cr2' if raw else 'jpg'}"
        data = f"shot {name}".encode()
        (folder / name).write_bytes(data)
        fields = dict(capture_id=str(uuid.uuid4()), filename=name, file_path=str(folder / name),
                      sha256=hashlib.sha256(data).hexdigest(), captured_at=datetime(2026, 11, 22))
        if raw:
            pname = name.rsplit(".", 1)[0] + "_preview.jpg"
            pdata = f"preview {pname}".encode()
            (folder / pname).write_bytes(pdata)
            fields.update(preview_filename=pname, preview_path=str(folder / pname),
                          preview_sha256=hashlib.sha256(pdata).hexdigest())
        shot = ColourCardShot(set_id=s.id, camera_index=cam, **fields)
        db.add(shot)
        db.commit()
        out.append(shot)
    return out


def _card_rows(db):
    from app.models.colour_card import ColourCardObject
    db.expire_all()
    return {(r.shot_id, r.kind): r for r in db.query(ColourCardObject).all()}


@pytest.mark.unit
def test_a_taken_sets_shots_and_previews_are_uploaded_to_the_calibration_folder(remote, db_session,
                                                                                override_projects_root):
    from app.models.offsite_object import OffsiteObject
    vol = _volume(db_session)
    page = _image(db_session, vol, override_projects_root, "f1.jpg", b"page")
    shots = _shots(db_session, vol, override_projects_root, raw=True)

    result = _run(db_session)

    assert result["status"] == "done", result
    assert result["uploaded"] == 5
    files = remote.files()
    for shot in shots:
        assert files[f"{TENANT}/{STEM}/calibration/{shot.filename}"] == (
            override_projects_root / STEM / "calibration" / shot.filename).read_bytes()
        assert f"{TENANT}/{STEM}/calibration/{shot.preview_filename}" in files
    rows = _card_rows(db_session)
    assert set(rows) == {(s.id, k) for s in shots for k in ("shot", "preview")}
    assert all(r.verified_at is not None and r.uploaded_at is not None for r in rows.values())
    # The page's row is the only offsite_objects row.
    assert [(o.record_image_id, o.kind) for o in db_session.query(OffsiteObject).all()] == [(page.id, "capture")]
    again = _run(db_session)
    assert again["uploaded"] == 0 and again["already_present"] == 0


@pytest.mark.unit
@pytest.mark.parametrize("outcome", [None, "skipped"])
def test_an_open_or_skipped_sets_shots_are_not_uploaded(remote, db_session, override_projects_root, outcome):
    vol = _volume(db_session)
    _shots(db_session, vol, override_projects_root, outcome=outcome)
    assert _run(db_session)["uploaded"] == 0
    assert _card_rows(db_session) == {}


@pytest.mark.unit
def test_a_kept_volumes_shots_are_not_uploaded(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    _shots(db_session, vol, override_projects_root)
    vol.kept_on_unit = True
    db_session.commit()
    assert _run(db_session)["uploaded"] == 0
    assert _card_rows(db_session) == {}


@pytest.mark.unit
def test_a_shot_whose_bytes_changed_is_refused_for_fixity(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    shots = _shots(db_session, vol, override_projects_root)
    (override_projects_root / STEM / "calibration" / shots[0].filename).write_bytes(b"other bytes")
    result = _run(db_session)
    assert [(r["colour_card_shot_id"], r["reason"]) for r in result["refused"]] == [(shots[0].id, "fixity")]
    rows = _card_rows(db_session)
    assert rows[(shots[0].id, "shot")].refusal == "fixity"
    assert rows[(shots[1].id, "shot")].verified_at is not None


@pytest.mark.unit
def test_an_object_already_at_the_key_with_other_bytes_is_never_overwritten(remote, db_session,
                                                                            override_projects_root):
    vol = _volume(db_session)
    shots = _shots(db_session, vol, override_projects_root)
    key = f"{TENANT}/{STEM}/calibration/{shots[0].filename}"
    remote.put(key, b"someone else's")
    result = _run(db_session)
    assert [r["reason"] for r in result["refused"]] == ["remote_differs"]
    assert remote.files()[key] == b"someone else's"


@pytest.mark.unit
def test_freeing_counts_no_shot(remote, db_session, override_projects_root, monkeypatch):
    from app.core import unit_mode
    from app.core.offsite_free import _objects, freeable_volumes
    monkeypatch.setattr(unit_mode, "offsite_on", lambda db: True)
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "xx-test-001-img_0001_c0.jpg", b"page")
    _run(db_session)
    before = freeable_volumes(db_session)
    _shots(db_session, vol, override_projects_root, raw=True)
    _run(db_session)
    assert len(_card_rows(db_session)) == 4
    after = freeable_volumes(db_session)
    assert after["volumes"] == before["volumes"] and after["total"] == before["total"]
    assert [o.kind for o in _objects(db_session, vol.id, freed=False)] == ["capture"]


# ---------------------------------------------------------------------------
# The export bag
# ---------------------------------------------------------------------------

def _bag_volume(db, root):
    """An approved single-image volume whose page has its capture record, as the export needs."""
    import json
    from app.models.record import Record, RecordImage
    vol = _volume(db)
    data = b"page bytes"
    page = root / STEM / "captures" / f"{STEM}-img_0001_c0.jpg"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(data)
    (root / STEM / "metadata").mkdir(parents=True, exist_ok=True)
    with (root / STEM / "metadata" / "manifest.jsonl").open("a") as fh:
        fh.write(json.dumps({"capture_id": "cid1", "files": [{"role": "single", "relative_path":
                             f"captures/{page.name}", "sha256": hashlib.sha256(data).hexdigest()}]}) + "\n")
    rec = Record(title="r", collection_id=vol.id, status="approved", capture_mode="single", sequence=1)
    db.add(rec)
    db.commit()
    db.add(RecordImage(record_id=rec.id, filename=page.name, file_path=str(page), format="jpg", role="single",
                       capture_id="cid1", is_current=True))
    db.commit()
    return vol


@pytest.fixture
def exporter(client, db_session, monkeypatch, tmp_path):
    from app.core import config
    from app.main import app
    import app.api.collections as collections
    from tests.unit.test_export_integrity import _contributor
    exports = tmp_path / "exp"
    exports.mkdir()
    monkeypatch.setattr(config.settings, "EXPORTS_ROOT", str(exports))
    app.dependency_overrides[collections.allow_read_only] = lambda: _contributor()
    return client, exports


@pytest.mark.unit
def test_the_bag_carries_the_taken_shots_in_its_calibration_folder(exporter, db_session, override_projects_root):
    import json
    import zipfile
    import bagit
    from tests.unit.test_export_integrity import _run_to_completion
    client, exports = exporter
    vol = _bag_volume(db_session, override_projects_root)
    shots = _shots(db_session, vol, override_projects_root, raw=True)
    _shots(db_session, vol, override_projects_root, outcome="skipped")
    resp = client.post(f"/collections/{vol.id}/export")
    assert resp.status_code == 202, resp.text
    st = _run_to_completion(client, vol.id, resp.json()["job_id"])
    assert st["state"] == "done", st
    zpath = exports / st["zip_filename"]
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        meta = json.loads(zf.read(next(n for n in names if n.endswith("data/metadata.json"))))
        zf.extractall(exports / "x")
    bagged = sorted(n.split("data/", 1)[1] for n in names if "/data/calibration/" in n)
    assert bagged == sorted([f"calibration/{s.filename}" for s in shots]
                            + [f"calibration/{s.preview_filename}" for s in shots])
    assert meta["schema_version"] == 3
    assert [[x["capture_id"] for x in s["shots"]] for s in meta["colour_card"]["sets"]] == [
        [s.capture_id for s in shots]]
    bagit.Bag(str(exports / "x" / st["zip_filename"][:-4])).validate()


@pytest.mark.unit
def test_an_export_with_a_shot_missing_from_disk_is_refused(exporter, db_session, override_projects_root):
    from tests.unit.test_export_integrity import _run_to_completion
    client, _exports = exporter
    vol = _bag_volume(db_session, override_projects_root)
    shots = _shots(db_session, vol, override_projects_root)
    (override_projects_root / STEM / "calibration" / shots[1].filename).unlink()
    resp = client.post(f"/collections/{vol.id}/export")
    st = _run_to_completion(client, vol.id, resp.json()["job_id"])
    assert st["state"] == "failed"
    assert st["detail"] == {"reason": "missing_colour_card_files", "missing_colour_card_shot_ids": [shots[1].id]}


# ---------------------------------------------------------------------------
# A shot's first claim and a keep on the unit take turns, as a page's do
# ---------------------------------------------------------------------------

def _keep(client, vid):
    from tests.unit.test_capture_manifest_export import _as
    return _as(client, "admin").post(f"/collections/{vid}/keep-on-unit")


@pytest.mark.unit
def test_a_keep_committed_before_a_shots_first_claim_stops_its_upload(client, remote, db_session,
                                                                      override_projects_root, monkeypatch):
    from app.core import image_upload
    vol = _volume(db_session)
    _shots(db_session, vol, override_projects_root)
    real_listed = image_upload.listed_object
    kept = []

    def listed_after_a_keep(key):
        if not kept:
            resp = _keep(client, vol.id)
            assert resp.status_code == 200, resp.text
            kept.append(resp)
        return real_listed(key)

    monkeypatch.setattr(image_upload, "listed_object", listed_after_a_keep)
    result = image_upload.run_image_upload(db_session)
    assert result["uploaded"] == 0 and remote.calls("copyto") == []
    assert _card_rows(db_session) == {}


@pytest.mark.unit
def test_a_keep_during_a_shots_copy_is_refused_and_the_copy_completes(client, remote, db_session,
                                                                      override_projects_root, monkeypatch):
    from app.core import image_upload
    vol = _volume(db_session)
    _shots(db_session, vol, override_projects_root)
    answers = []
    real_upload = image_upload.upload_file

    def upload_with_a_keep(local, key):
        answers.append(_keep(client, vol.id))
        return real_upload(local, key)

    monkeypatch.setattr(image_upload, "upload_file", upload_with_a_keep)
    result = image_upload.run_image_upload(db_session)
    assert result["uploaded"] == 2, result
    assert answers[0].status_code == 409 and answers[0].json()["detail"]["code"] == "already_left_unit"


@pytest.mark.unit
def test_a_shots_first_claim_takes_the_volume_row_for_share(remote, db_session, override_projects_root, monkeypatch):
    from sqlalchemy.orm import Query
    from app.core import image_upload
    vol = _volume(db_session)
    shots = _shots(db_session, vol, override_projects_root)
    state = image_upload._card_state(db_session, shots[0].id)
    asked = []
    real = Query.with_for_update

    def spy(self, *a, **kw):
        asked.append((self.column_descriptions[0]["entity"].__name__, kw))
        return real(self, *a, **kw)

    monkeypatch.setattr(Query, "with_for_update", spy)
    image_upload._claim_card(db_session, None, image_upload.CardWork(shots[0].id, "shot"),
                             f"{TENANT}/{STEM}/calibration/{shots[0].filename}", state, local_sha1="0" * 40)
    assert asked == [("Collection", {"read": True})]


# ---------------------------------------------------------------------------
# A shot the uploader keeps skipping says why, and stops holding its volume
# ---------------------------------------------------------------------------

def _set_of(db, shot):
    from app.models.colour_card import ColourCardSet, ColourCardShot
    db.expire_all()
    return db.get(ColourCardSet, db.get(ColourCardShot, shot.id).set_id)


def _delivery_of(db, shot):
    from app.core import colour_card
    return colour_card.delivery(db, _set_of(db, shot))


@pytest.mark.unit
@pytest.mark.parametrize("path,code", [("_stage", "staging_failed"), ("stable_digests", "file_unstable")])
def test_each_skip_records_its_reason(remote, db_session, override_projects_root, monkeypatch, path, code):
    from app.core import image_upload
    vol = _volume(db_session)
    shots = _shots(db_session, vol, override_projects_root)
    monkeypatch.setattr(image_upload, path, lambda p: None)
    _run(db_session)
    rows = _card_rows(db_session)
    assert {(k, r.last_skip_reason, r.skip_count, r.refused_at) for k, r in rows.items()} == {
        ((s.id, "shot"), code, 1, None) for s in shots}
    assert _delivery_of(db_session, shots[0]) == ("waiting", code)


@pytest.mark.unit
def test_a_listing_without_a_checksum_records_unverified_then_listed_without_sha1(remote, db_session,
                                                                               override_projects_root):
    vol = _volume(db_session)
    shots = _shots(db_session, vol, override_projects_root)
    remote.set_modes("strip-hashes", "strip-always")
    _run(db_session)
    assert {r.last_skip_reason for r in _card_rows(db_session).values()} == {"unverified"}
    _run(db_session)
    rows = _card_rows(db_session)
    assert {(r.last_skip_reason, r.skip_count) for r in rows.values()} == {("listed_without_sha1", 1)}
    assert _delivery_of(db_session, shots[0]) == ("waiting", "listed_without_sha1")


@pytest.mark.unit
def test_ten_runs_skipping_for_one_reason_make_the_shot_undeliverable(remote, db_session,
                                                                      override_projects_root, monkeypatch):
    from app.core import colour_card, image_upload
    vol = _volume(db_session)
    shots = _shots(db_session, vol, override_projects_root)
    monkeypatch.setattr(image_upload, "_stage", lambda p: None)
    for n in range(1, 10):
        _run(db_session)
        assert {r.skip_count for r in _card_rows(db_session).values()} == {n}
        assert all(r.refused_at is None for r in _card_rows(db_session).values())
    _run(db_session)
    rows = _card_rows(db_session)
    assert {(r.refusal, r.refused_at is not None) for r in rows.values()} == {("staging_failed", True)}
    assert _delivery_of(db_session, shots[0]) == ("undeliverable", "upload_refused")
    # Nothing is waited for any more: the volume can be sent, `skipped`.
    db_session.expire_all()
    result = colour_card.evaluate(db_session, vol, TENANT, [{"_image_id": 10 ** 6, "captureId": "p"}])
    assert result.waiting == 0 and result.status == "skipped"
    calls = len(remote.calls())
    _run(db_session)
    assert len(remote.calls()) == calls, "a refused shot is not tried again"


@pytest.mark.unit
def test_a_success_resets_the_count(remote, db_session, override_projects_root, monkeypatch):
    from app.core import image_upload
    vol = _volume(db_session)
    _shots(db_session, vol, override_projects_root)
    real = image_upload._stage
    monkeypatch.setattr(image_upload, "_stage", lambda p: None)
    for _ in range(9):
        _run(db_session)
    monkeypatch.setattr(image_upload, "_stage", real)
    _run(db_session)
    rows = _card_rows(db_session)
    assert {(r.verified_at is not None, r.skip_count, r.last_skip_reason) for r in rows.values()} == {(True, 0, None)}


@pytest.mark.unit
def test_another_reason_starts_the_count_again(remote, db_session, override_projects_root, monkeypatch):
    from app.core import image_upload
    vol = _volume(db_session)
    _shots(db_session, vol, override_projects_root)
    real = image_upload._stage
    monkeypatch.setattr(image_upload, "_stage", lambda p: None)
    for _ in range(5):
        _run(db_session)
    monkeypatch.setattr(image_upload, "_stage", real)
    monkeypatch.setattr(image_upload, "stable_digests", lambda p: None)
    _run(db_session)
    assert {(r.last_skip_reason, r.skip_count) for r in _card_rows(db_session).values()} == {("file_unstable", 1)}


@pytest.mark.unit
def test_a_shot_that_was_never_read_does_not_stop_a_keep(client, remote, db_session, override_projects_root,
                                                         monkeypatch):
    from app.core import image_upload
    vol = _volume(db_session)
    _shots(db_session, vol, override_projects_root)
    monkeypatch.setattr(image_upload, "_stage", lambda p: None)
    _run(db_session)
    assert _keep(client, vol.id).status_code == 200


# ---------------------------------------------------------------------------
# The export's free-space check counts the shots
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_export_without_room_for_the_shots_is_refused_before_anything_is_written(
        exporter, db_session, override_projects_root, monkeypatch):
    import collections
    from app.core import export_service
    from tests.unit.test_export_integrity import _run_to_completion
    client, exports = exporter
    vol = _bag_volume(db_session, override_projects_root)
    _shots(db_session, vol, override_projects_root, raw=True)
    pages = (override_projects_root / STEM / "captures" / f"{STEM}-img_0001_c0.jpg").stat().st_size
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(export_service.shutil, "disk_usage",
                        lambda p: usage(10 ** 12, 0, pages + export_service.FREE_SPACE_MARGIN + 1))
    resp = client.post(f"/collections/{vol.id}/export")
    st = _run_to_completion(client, vol.id, resp.json()["job_id"])
    assert st["state"] == "failed" and st["detail"]["reason"] == "low_disk", st
    assert list(exports.glob("*.zip*")) == []


@pytest.mark.unit
def test_a_keep_before_the_claim_stops_a_shot_that_has_a_skip_row(client, remote, db_session,
                                                                  override_projects_root, monkeypatch):
    """A row that only records a skip is no evidence the volume was checked: the next claim still checks it."""
    from app.core import image_upload
    vol = _volume(db_session)
    _shots(db_session, vol, override_projects_root)
    real_stage = image_upload._stage
    monkeypatch.setattr(image_upload, "_stage", lambda p: None)
    _run(db_session)
    rows = _card_rows(db_session)
    assert len(rows) == 2 and all(r.local_sha1 is None for r in rows.values())
    monkeypatch.setattr(image_upload, "_stage", real_stage)
    real_listed = image_upload.listed_object
    kept = []

    def listed_after_a_keep(key):
        if not kept:
            resp = _keep(client, vol.id)
            assert resp.status_code == 200, resp.text
            kept.append(resp)
        return real_listed(key)

    monkeypatch.setattr(image_upload, "listed_object", listed_after_a_keep)
    result = image_upload.run_image_upload(db_session)
    assert kept, "the keep was made during the run"
    assert result["uploaded"] == 0 and remote.calls("copyto") == []
    rows = _card_rows(db_session)
    assert all(r.local_sha1 is None and r.uploaded_at is None and r.verified_at is None for r in rows.values())
