"""Capture manifest export (contract 1.1, artefact 2): GET /collections/{id}/capture-manifest.

The manifest is built from the catalogue, the capture manifests
(`<stem>/metadata/manifest.jsonl`) and offsite_objects, never from the image
files. Each refusal here is one that Fisqua's registration
(fisqua-dev scripts/lib/capture-registration.ts) would make, or one the
contract's §4 shape requires; the export answers 409 and names the records.

No bucket and no rclone: offsite_objects rows are written directly, as the
uploader leaves them.

    python -m pytest tests/unit/test_capture_manifest_export.py
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

TENANT = "captua-test"
STEM = "xx-test-001"
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _as(client, role, username=None):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username or role, role)
    return client


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class Volume:
    """A stemmed volume on disk and in the catalogue, captured and uploaded as the unit leaves it."""

    def __init__(self, db, projects_dir: Path, stem=STEM, name="Tomo 55", external_id=STEM,
                 project_name="Fondo Concejo", tenant=TENANT):
        from app.models.collection import Collection
        from app.models.project import Project

        self.db = db
        self.tenant = tenant
        self.stem = stem
        self.root = projects_dir / stem
        for sub in ("captures", "previews", "metadata"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        project = db.query(Project).filter(Project.name == project_name).one_or_none()
        if project is None:
            project = Project(name=project_name)
            db.add(project)
            db.commit()
        self.project = project
        self.col = Collection(name=name, project_id=project.id, filename_stem=stem,
                              external_id=external_id)
        db.add(self.col)
        db.commit()
        db.refresh(self.col)
        self.ordinal = 0

    # -- manifest lines -------------------------------------------------

    def manifest_line(self, entry: dict) -> None:
        with (self.root / "metadata" / "manifest.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    # -- records ----------------------------------------------------------

    def record(self, status="approved", sequence=None, layout=("left", "right"), ext="jpg",
               preview_digest_recorded=True, upload=True, record_digest=True, capture_id=True,
               rejection=None):
        """A record with one capture event: one image per role in `layout`.

        For a RAW `ext`, each image gets a `_preview.jpg` sidecar and a
        preview object. `preview_digest_recorded` lists that preview, with
        its sha256, in the capture record's `files` (see the note on
        test_refused_with_a_preview_that_has_no_recorded_digest).
        """
        from app.models.record import Record, RecordImage, RecordRejection

        if sequence is None:
            sequence = len(self.col.records) + 1
        rec = Record(title=f"r{sequence}", collection_id=self.col.id, status=status,
                     sequence=sequence, capture_mode="dual" if len(layout) == 2 else "single")
        self.db.add(rec)
        self.db.commit()
        self.db.refresh(rec)
        if rejection is not None:
            self.db.add(RecordRejection(record_id=rec.id, predefined_reason=rejection[0], comment=rejection[1]))
            self.db.commit()
        self.capture(rec, layout, ext=ext, preview_digest_recorded=preview_digest_recorded,
                     upload=upload, record_digest=record_digest, capture_id=capture_id)
        self.db.refresh(rec)
        return rec

    def capture(self, rec, layout, ext="jpg", preview_digest_recorded=True, upload=True,
                record_digest=True, capture_id=True):
        """One capture event into `rec`: files, a capture record, image rows, offsite rows."""
        from app.models.record import RecordImage

        self.ordinal += 1
        n = self.ordinal
        cid = str(uuid.uuid4()) if capture_id else None
        pair = f"{self.stem}-img_{n:04d}"
        files = []
        images = []
        for cam, role in enumerate(layout):
            name = f"{self.stem}-img_{n:04d}_c{cam}.{ext}"
            data = f"{name} bytes".encode()
            path = self.root / "captures" / name
            path.write_bytes(data)
            entry = {"role": role, "relative_path": f"captures/{name}", "bytes": len(data)}
            if record_digest:
                entry["sha256"] = _sha256(data)
            files.append(entry)
            preview = None
            from app.core.paths import RAW_SUFFIXES
            if f".{ext.lower()}" in RAW_SUFFIXES:
                pname = f"{self.stem}-img_{n:04d}_c{cam}_preview.jpg"
                pdata = f"{pname} bytes".encode()
                (self.root / "previews" / pname).write_bytes(pdata)
                preview = (pname, pdata)
                if preview_digest_recorded:
                    files.append({"role": f"{role}_preview", "relative_path": f"previews/{pname}",
                                  "bytes": len(pdata), "sha256": _sha256(pdata)})
            img = RecordImage(record_id=rec.id, capture_id=cid, pair_id=pair, role=role,
                              filename=name, file_path=str(path), format=ext, is_current=True)
            self.db.add(img)
            self.db.commit()
            self.db.refresh(img)
            images.append((img, data, preview))
        if cid is not None:
            self.manifest_line({"capture_id": cid, "pair_id": pair, "files": files, "status": "success"})
        if upload:
            for img, data, preview in images:
                self.offsite(img, "capture", f"{self.tenant}/{self.stem}/captures/{img.filename}", data)
                if preview is not None:
                    self.offsite(img, "preview", f"{self.tenant}/{self.stem}/previews/{preview[0]}", preview[1])
        return [i for i, _, _ in images]

    def offsite(self, img, kind, key, data, state="verified", refusal=None):
        from app.models.offsite_object import OffsiteObject

        now = datetime.now(timezone.utc)
        row = OffsiteObject(record_image_id=img.id, kind=kind, object_key=key, local_sha1=_sha1(data))
        if state == "verified":
            row.uploaded_at, row.verified_at = now, now
        elif state == "refused":
            row.refused_at, row.refusal = now, refusal or "remote_differs"
            row.remote_sha1 = "0" * 40
        elif state == "uploaded":
            row.uploaded_at = now
        self.db.add(row)
        self.db.commit()
        return row

    def row(self, img, kind):
        from app.models.offsite_object import OffsiteObject
        return (self.db.query(OffsiteObject)
                .filter(OffsiteObject.record_image_id == img.id, OffsiteObject.kind == kind).one())

    def supersede(self, rec):
        """Mark every current image of `rec` superseded, as a retake does."""
        for img in rec.images:
            if img.is_current:
                img.is_current = False
                img.superseded_at = datetime.now(timezone.utc)
        self.db.commit()


@pytest.fixture
def tenant(db_session, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    return TENANT


@pytest.fixture
def vol(db_session, override_projects_root, tenant):
    return Volume(db_session, override_projects_root)


def _export(client, col, role="operator"):
    return _as(client, role).get(f"/collections/{col.id}/capture-manifest")


def _refusal(resp):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail["message"], str) and detail["message"]
    return {p["rule"]: p for p in detail["problems"]}


def _images(doc):
    return [i for r in doc["volumes"][0]["records"] for i in r["images"]]


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator"])
def test_admin_and_operator_can_export(client, vol, role):
    vol.record()
    assert _export(client, vol.col, role).status_code == 200


@pytest.mark.unit
def test_reviewer_cannot_export(client, vol):
    vol.record()
    assert _export(client, vol.col, "reviewer").status_code == 403


@pytest.mark.unit
def test_unknown_volume_is_404(client, vol):
    assert _as(client, "operator").get("/collections/9999/capture-manifest").status_code == 404


# ---------------------------------------------------------------------------
# The envelope (contract §2, §4; capture-registration.ts manifestSchema)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_envelope(client, vol):
    vol.record()
    resp = _export(client, vol.col)
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    assert doc["contract"] == "fisqua-captua-exchange"
    assert doc["version"] == "1.1"
    assert doc["kind"] == "capture-manifest"
    assert UUID4.match(doc["id"])
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", doc["generatedAt"])
    assert doc["generatedBy"]["system"] == "captua"
    assert doc["generatedBy"]["instance"] == TENANT
    assert isinstance(doc["generatedBy"]["systemVersion"], str)
    assert doc["inReplyTo"] is None
    assert doc["project"] == {"captuaProjectId": vol.project.id, "name": "Fondo Concejo"}
    assert doc["transfer"] == {"channel": "upload", "root": f"{TENANT}/"}
    [v] = doc["volumes"]
    assert v == {**v, "externalId": STEM, "captuaCollectionId": vol.col.id, "stem": STEM, "title": "Tomo 55"}


@pytest.mark.unit
def test_download_is_named_for_the_stem_and_date(client, vol):
    vol.record()
    resp = _export(client, vol.col)
    today = resp.json()["generatedAt"][:10]
    assert resp.headers["content-disposition"] == f'attachment; filename="capture-manifest_{STEM}_{today}.json"'
    assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.unit
def test_each_export_has_a_fresh_id(client, vol):
    vol.record()
    a, b = _export(client, vol.col).json()["id"], _export(client, vol.col).json()["id"]
    assert a != b


@pytest.mark.unit
def test_each_export_is_logged_with_volume_id_and_counts(client, vol, db_session):
    from app.models.system_log import SystemLog

    vol.record(layout=("left", "right"), ext="cr2")
    vol.record(layout=("single",))
    doc = _export(client, vol.col).json()
    rows = db_session.query(SystemLog).filter(SystemLog.action == "capture_manifest_exported").all()
    assert len(rows) == 1
    row = rows[0]
    assert row.level == "INFO" and row.actor == "operator"
    assert row.subject == STEM
    assert doc["id"] in row.detail
    assert f"volume {vol.col.id}" in row.detail
    assert "records=2" in row.detail and "images=3" in row.detail and "previews=2" in row.detail


@pytest.mark.unit
def test_a_volume_with_no_recorded_reference_code_travels_with_its_stem(client, db_session, override_projects_root, tenant):
    # Contract 1.2 §8: a volume's filenameStem is its Fisqua reference code,
    # unchanged, so the stem stands in when external_id is not recorded.
    v = Volume(db_session, override_projects_root, external_id=None)
    v.record()
    resp = _export(client, v.col)
    assert resp.status_code == 200, resp.text
    assert resp.json()["volumes"][0]["externalId"] == v.stem


@pytest.mark.unit
def test_a_recorded_reference_code_travels_as_it_is(client, db_session, override_projects_root, tenant):
    """The model refuses a code other than the stem (backend #140); a row that already holds one travels as recorded."""
    import sqlalchemy as sa
    v = Volume(db_session, override_projects_root, external_id=None)
    db_session.execute(sa.text("UPDATE collections SET external_id = 'xx-other-009' WHERE id = :id"), {"id": v.col.id})
    db_session.commit()
    db_session.expire_all()
    v.record()
    resp = _export(client, v.col)
    assert resp.status_code == 200, resp.text
    assert resp.json()["volumes"][0]["externalId"] == "xx-other-009"


# ---------------------------------------------------------------------------
# Records and images
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_keys_are_the_frozen_offsite_keys_relative_to_the_transfer_root(client, vol):
    rec = vol.record(layout=("single",), ext="cr2")
    [img] = rec.images
    # The file is renamed on disk after upload, as a relocation does, and the
    # relocation record carries its digest; the key stays as it was.
    old = Path(img.file_path)
    digest = _sha256(old.read_bytes())
    old.rename(old.with_name("moved.cr2"))
    img.file_path = str(old.with_name("moved.cr2"))
    img.filename = "moved.cr2"
    vol.db.commit()
    vol.manifest_line({"record_type": "relocation", "capture_id": img.capture_id, "image_id": img.id,
                       "sha256": digest, "from": f"captures/{old.name}", "to": "captures/moved.cr2"})
    doc = _export(client, vol.col).json()
    [out] = _images(doc)
    assert out["objectKey"] == f"{STEM}/captures/{STEM}-img_0001_c0.cr2"
    assert out["filename"] == f"{STEM}-img_0001_c0.cr2"
    assert out["previewObjectKey"] == f"{STEM}/previews/{STEM}-img_0001_c0_preview.jpg"
    assert out["sha256"] == digest
    assert not out["objectKey"].startswith(TENANT)


@pytest.mark.unit
def test_image_fields(client, vol):
    rec = vol.record(layout=("left", "right"))
    doc = _export(client, vol.col).json()
    [r] = doc["volumes"][0]["records"]
    assert r["captuaRecordId"] == rec.id and r["sequence"] == 1
    assert r["status"] == "approved" and r["rejectionNote"] is None
    left, right = r["images"]
    assert set(left) == {"captureId", "pairId", "filename", "role", "objectKey", "sha256"}
    assert left["role"] == "left" and right["role"] == "right"
    assert left["captureId"] == right["captureId"] == rec.images[0].capture_id
    assert left["pairId"] == f"{STEM}-img_0001"


@pytest.mark.unit
def test_digests_come_from_the_capture_manifest_not_the_file(client, vol):
    rec = vol.record(layout=("single",), ext="cr2")
    [img] = rec.images
    expected = _sha256(Path(img.file_path).read_bytes())
    preview = vol.root / "previews" / f"{STEM}-img_0001_c0_preview.jpg"
    expected_preview = _sha256(preview.read_bytes())
    Path(img.file_path).write_bytes(b"changed on disk")
    preview.write_bytes(b"changed on disk")
    [out] = _images(_export(client, vol.col).json())
    assert out["sha256"] == expected
    assert out["previewSha256"] == expected_preview


@pytest.mark.unit
def test_a_freed_volume_exports_from_rows_and_keys(client, vol):
    rec = vol.record(layout=("left", "right"), ext="cr2")
    for p in list((vol.root / "captures").iterdir()) + list((vol.root / "previews").iterdir()):
        p.unlink()
    resp = _export(client, vol.col)
    assert resp.status_code == 200, resp.text
    out = _images(resp.json())
    assert [i["role"] for i in out] == ["left", "right"]
    assert all(SHA256.match(i["sha256"]) and SHA256.match(i["previewSha256"]) for i in out)


@pytest.mark.unit
def test_only_current_images_are_listed(client, vol):
    rec = vol.record(layout=("single",))
    old = list(rec.images)
    vol.supersede(rec)
    new = vol.capture(rec, ("single",))
    doc = _export(client, vol.col).json()
    [out] = _images(doc)
    assert out["filename"] == new[0].filename == f"{STEM}-img_0002_c0.jpg"
    assert old[0].filename not in json.dumps(doc)


@pytest.mark.unit
def test_records_in_sequence_order_with_every_status(client, vol):
    b = vol.record(status="in_review", sequence=2)
    a = vol.record(status="approved", sequence=1)
    c = vol.record(status="rejected", sequence=3, rejection=("blur", "folio 3 blurred"))
    d = vol.record(status="rejected", sequence=4, rejection=("glare", None))
    records = _export(client, vol.col).json()["volumes"][0]["records"]
    assert [r["captuaRecordId"] for r in records] == [a.id, b.id, c.id, d.id]
    assert [r["status"] for r in records] == ["approved", "in_review", "rejected", "rejected"]
    assert records[2]["rejectionNote"] == "folio 3 blurred"
    assert records[3]["rejectionNote"] == "glare"


@pytest.mark.unit
def test_the_new_reasons_travel_as_the_old_ones_do(client, vol):
    vol.record(status="approved", sequence=1)  # Fisqua needs one page to register
    vol.record(status="rejected", sequence=5, rejection=("framing", None))
    vol.record(status="rejected", sequence=2, rejection=("obstruction", "  "))
    vol.record(status="rejected", sequence=3, rejection=("obstruction", "weight on the gutter"))
    vol.record(status="rejected", sequence=4, rejection=("other", "folio out of order"))
    resp = _export(client, vol.col)
    assert resp.status_code == 200, resp.text
    records = resp.json()["volumes"][0]["records"]
    assert [r["rejectionNote"] for r in records] == [
        None, "obstruction", "weight on the gutter", "folio out of order", "framing"]


@pytest.mark.unit
def test_a_rejected_record_whose_images_were_all_superseded_travels_with_none(client, vol):
    vol.record()
    rej = vol.record(status="rejected", rejection=("blur", None))
    vol.supersede(rej)
    resp = _export(client, vol.col)
    assert resp.status_code == 200, resp.text
    assert resp.json()["volumes"][0]["records"][1]["images"] == []


@pytest.mark.unit
def test_a_jpeg_capture_has_no_preview_fields(client, vol):
    vol.record(layout=("single",))
    [out] = _images(_export(client, vol.col).json())
    assert "previewObjectKey" not in out and "previewSha256" not in out


@pytest.mark.unit
def test_refused_with_a_raw_capture_that_has_no_preview(client, vol):
    # Fisqua's runner reads pages with Pillow and has no RAW decoder, so a RAW
    # with no preview has nothing Fisqua can read.
    vol.record()
    rec = vol.record(layout=("single",), ext="cr2", preview_digest_recorded=False)
    [img] = rec.images
    vol.db.delete(vol.row(img, "preview"))
    vol.db.commit()
    (vol.root / "previews" / f"{Path(img.filename).stem}_preview.jpg").unlink()
    problems = _refusal(_export(client, vol.col))
    assert problems["raw_without_preview"]["record_ids"] == [rec.id]
    assert problems["raw_without_preview"]["image_ids"] == [img.id]
    assert "captured again" in problems["raw_without_preview"]["message"]


@pytest.mark.unit
def test_a_superseded_raw_without_a_preview_does_not_block(client, vol):
    rec = vol.record(layout=("single",), ext="cr2", upload=False, preview_digest_recorded=False)
    [old] = rec.images
    (vol.root / "previews" / f"{STEM}-img_0001_c0_preview.jpg").unlink()
    vol.supersede(rec)
    vol.capture(rec, ("single",), ext="cr2")
    assert _export(client, vol.col).status_code == 200


# ---------------------------------------------------------------------------
# Refusals: the unit and the volume
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_refused_with_no_tenant(client, vol, monkeypatch):
    from app.core import config
    vol.record()
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "  ")
    problems = _refusal(_export(client, vol.col))
    assert "tenant_missing" in problems
    assert "CAPTUA_OFFSITE_TENANT" in problems["tenant_missing"]["message"]


@pytest.mark.unit
def test_refused_with_a_tenant_that_is_not_a_prefix(client, vol, monkeypatch):
    from app.core import config
    vol.record()
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "Captua Test")
    assert "tenant_invalid" in _refusal(_export(client, vol.col))


@pytest.mark.unit
def test_refused_with_no_stem(client, db_session, tenant):
    from app.models.collection import Collection
    from app.models.project import Project
    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    col = Collection(name="Tomo", project_id=p.id)
    db_session.add(col)
    db_session.commit()
    problems = _refusal(_export(client, col))
    assert "no_stem" in problems
    assert "stem" in problems["no_stem"]["message"]


@pytest.mark.unit
def test_refused_with_a_null_sequence(client, vol):
    a = vol.record(sequence=1)
    b = vol.record(sequence=2)
    b.sequence = None
    vol.db.commit()
    problems = _refusal(_export(client, vol.col))
    assert problems["sequence_missing"]["record_ids"] == [b.id]
    assert "Renumber the volume" in problems["sequence_missing"]["message"]


@pytest.mark.unit
def test_refused_with_a_repeated_sequence_whatever_the_status(client, vol):
    a = vol.record(sequence=1)
    b = vol.record(sequence=1, status="rejected", rejection=("blur", None))
    problems = _refusal(_export(client, vol.col))
    assert problems["sequence_repeated"]["record_ids"] == sorted([a.id, b.id])
    assert "Renumber the volume" in problems["sequence_repeated"]["message"]


@pytest.mark.unit
def test_refused_while_any_record_is_captured(client, vol):
    vol.record(status="approved")
    c = vol.record(status="captured")
    problems = _refusal(_export(client, vol.col))
    assert problems["records_captured"]["record_ids"] == [c.id]
    assert "Hand the volume over" in problems["records_captured"]["message"]


@pytest.mark.unit
def test_refused_with_no_page(client, vol):
    vol.record(status="rejected", rejection=("blur", None))
    assert "no_pages" in _refusal(_export(client, vol.col))


@pytest.mark.unit
def test_refused_when_empty(client, vol):
    assert "no_pages" in _refusal(_export(client, vol.col))


# ---------------------------------------------------------------------------
# Refusals: roles and names
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("layout", [("left", "left"), ("single", "left"), ("single", "single"),
                                    ("overview",), (None,), ("right", "left", "single")])
def test_refused_with_a_role_layout_fisqua_does_not_accept(client, vol, layout):
    ok = vol.record(layout=("left", "right"))
    bad = vol.record(layout=layout)
    problems = _refusal(_export(client, vol.col))
    assert problems["roles_invalid"]["record_ids"] == [bad.id]


@pytest.mark.unit
@pytest.mark.parametrize("layout", [("left",), ("right",), ("single",), ("right", "left")])
def test_accepted_role_layouts(client, vol, layout):
    vol.record(layout=layout)
    assert _export(client, vol.col).status_code == 200


@pytest.mark.unit
def test_refused_when_a_record_under_review_has_no_current_image(client, vol):
    vol.record()
    bare = vol.record(status="in_review")
    vol.supersede(bare)
    problems = _refusal(_export(client, vol.col))
    assert problems["roles_invalid"]["record_ids"] == [bare.id]


@pytest.mark.unit
def test_refused_with_a_name_not_of_the_contract_form(client, vol):
    rec = vol.record(layout=("single",), upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", f"{TENANT}/{STEM}/captures/folio_001.jpg", b"x")
    problems = _refusal(_export(client, vol.col))
    assert problems["filename_form"]["record_ids"] == [rec.id]
    assert "folio_001.jpg" in problems["filename_form"]["message"]


@pytest.mark.unit
def test_refused_with_a_disambiguated_key(client, vol):
    rec = vol.record(layout=("single",), upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", f"{TENANT}/{STEM}/captures/{STEM}-img_0001_c0_img{img.id}.jpg", b"x")
    assert "filename_form" in _refusal(_export(client, vol.col))


@pytest.mark.unit
@pytest.mark.parametrize("key", [
    f"{TENANT}/xx-other-002/captures/{STEM}-img_0001_c0.jpg",
    f"other-tenant/{STEM}/captures/{STEM}-img_0001_c0.jpg",
    f"{TENANT}/{STEM}/{STEM}-img_0001_c0.jpg",
    f"{TENANT}/{STEM}/previews/{STEM}-img_0001_c0.jpg",
    f"{TENANT}/{STEM}/captures/sub/{STEM}-img_0001_c0.jpg",
])
def test_refused_with_a_capture_key_outside_the_volume_captures(client, vol, key):
    rec = vol.record(layout=("single",), upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", key, b"x")
    problems = _refusal(_export(client, vol.col))
    assert problems["key_outside"]["record_ids"] == [rec.id]


@pytest.mark.unit
@pytest.mark.parametrize("name", [f"{STEM}-img_0009_c0_preview.jpg", f"{STEM}-img_0001_c0.jpg"])
def test_refused_with_a_preview_key_not_named_for_its_capture(client, vol, name):
    rec = vol.record(layout=("single",), ext="cr2", upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", f"{TENANT}/{STEM}/captures/{img.filename}", b"x")
    vol.offsite(img, "preview", f"{TENANT}/{STEM}/previews/{name}", b"y")
    assert "key_outside" in _refusal(_export(client, vol.col))


@pytest.mark.unit
def test_refused_with_two_images_under_one_page_key(client, vol):
    a = vol.record(layout=("single",), upload=False)
    b = vol.record(layout=("single",), upload=False)
    [ia], [ib] = a.images, b.images
    vol.offsite(ia, "capture", f"{TENANT}/{STEM}/captures/{STEM}-img_0001_c0.jpg", b"a")
    vol.offsite(ib, "capture", f"{TENANT}/{STEM}/captures/{STEM}-img_0001_c0.tif", b"b")
    problems = _refusal(_export(client, vol.col))
    assert problems["page_key_repeated"]["record_ids"] == sorted([a.id, b.id])


# ---------------------------------------------------------------------------
# Refusals: offsite objects
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_refused_with_an_image_never_uploaded(client, vol):
    vol.record()
    rec = vol.record(upload=False)
    problems = _refusal(_export(client, vol.col))
    assert problems["not_uploaded"]["record_ids"] == [rec.id]
    assert "offsite" in problems["not_uploaded"]["message"]


@pytest.mark.unit
@pytest.mark.parametrize("state", ["uploaded", "reserved"])
def test_refused_with_a_pending_object(client, vol, state):
    rec = vol.record(layout=("single",), upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", f"{TENANT}/{STEM}/captures/{img.filename}", b"x", state=state)
    assert _refusal(_export(client, vol.col))["not_uploaded"]["record_ids"] == [rec.id]


@pytest.mark.unit
@pytest.mark.parametrize("refusal", ["remote_differs", "fixity"])
def test_refused_with_a_refused_capture_object(client, vol, refusal):
    rec = vol.record(layout=("single",), upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", f"{TENANT}/{STEM}/captures/{img.filename}", b"x",
                state="refused", refusal=refusal)
    problems = _refusal(_export(client, vol.col))
    assert problems["upload_refused"]["record_ids"] == [rec.id]
    assert refusal in problems["upload_refused"]["message"]


@pytest.mark.unit
def test_refused_with_a_refused_preview_object(client, vol):
    rec = vol.record(layout=("single",), ext="cr2", upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", f"{TENANT}/{STEM}/captures/{img.filename}", b"x")
    vol.offsite(img, "preview", f"{TENANT}/{STEM}/previews/{STEM}-img_0001_c0_preview.jpg", b"y",
                state="refused")
    assert _refusal(_export(client, vol.col))["upload_refused"]["record_ids"] == [rec.id]


@pytest.mark.unit
def test_refused_with_a_pending_preview(client, vol):
    rec = vol.record(layout=("single",), ext="cr2", upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", f"{TENANT}/{STEM}/captures/{img.filename}", b"x")
    vol.offsite(img, "preview", f"{TENANT}/{STEM}/previews/{STEM}-img_0001_c0_preview.jpg", b"y",
                state="uploaded")
    assert _refusal(_export(client, vol.col))["preview_not_uploaded"]["record_ids"] == [rec.id]


@pytest.mark.unit
def test_refused_with_a_preview_on_disk_not_yet_uploaded(client, vol):
    rec = vol.record(layout=("single",), ext="cr2", upload=False)
    [img] = rec.images
    vol.offsite(img, "capture", f"{TENANT}/{STEM}/captures/{img.filename}", b"x")
    assert _refusal(_export(client, vol.col))["preview_not_uploaded"]["record_ids"] == [rec.id]


@pytest.mark.unit
def test_a_superseded_image_not_uploaded_does_not_block(client, vol):
    rec = vol.record(layout=("single",), upload=False)
    vol.supersede(rec)
    vol.capture(rec, ("single",))
    assert _export(client, vol.col).status_code == 200


# ---------------------------------------------------------------------------
# Refusals: digests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_refused_with_no_capture_id(client, vol):
    rec = vol.record(layout=("single",), capture_id=False)
    problems = _refusal(_export(client, vol.col))
    assert problems["capture_id_missing"]["record_ids"] == [rec.id]


@pytest.mark.unit
def test_refused_with_no_recorded_sha256(client, vol):
    vol.record()
    rec = vol.record(layout=("single",), record_digest=False)
    problems = _refusal(_export(client, vol.col))
    assert problems["sha256_missing"]["record_ids"] == [rec.id]


@pytest.mark.unit
def test_refused_with_a_malformed_recorded_sha256(client, vol):
    rec = vol.record(layout=("single",), record_digest=False)
    [img] = rec.images
    vol.manifest_line({"capture_id": img.capture_id, "files": [
        {"role": "single", "relative_path": f"captures/{img.filename}", "sha256": "ABC" + "0" * 61}]})
    assert _refusal(_export(client, vol.col))["sha256_missing"]["record_ids"] == [rec.id]


@pytest.mark.unit
def test_refused_with_a_preview_that_has_no_recorded_digest(client, vol):
    """A preview captured before capture recorded its sha256 has none until it is backfilled.

    The capture record lists a RAW's preview as `<role>_preview` beside its
    master (capture.manifestHandler._preview_file); that is the entry
    `preview_digest_recorded` writes in these tests."""
    rec = vol.record(layout=("left", "right"), ext="cr2", preview_digest_recorded=False)
    problems = _refusal(_export(client, vol.col))
    assert problems["preview_sha256_missing"]["record_ids"] == [rec.id]
    assert f"backfill-preview-digests {STEM} --apply" in problems["preview_sha256_missing"]["message"]


@pytest.mark.unit
def test_a_backfilled_preview_digest_is_read(client, vol):
    """The backfill's `preview_digest` record (app.core.preview_digests) is the other source."""
    rec = vol.record(layout=("single",), ext="cr2", preview_digest_recorded=False)
    [img] = rec.images
    name = f"{Path(img.filename).stem}_preview.jpg"
    digest = _sha256((vol.root / "previews" / name).read_bytes())
    vol.manifest_line({"record_type": "preview_digest", "capture_id": img.capture_id, "image_id": img.id,
                       "files": [{"role": "single_preview", "relative_path": f"previews/{name}",
                                  "bytes": 1, "mimetype": "image/jpeg", "sha256": digest}],
                       "computed_at": "2026-10-24T00:00:00+00:00", "backfilled": True})
    resp = _export(client, vol.col)
    assert resp.status_code == 200, resp.text
    [out] = _images(resp.json())
    assert out["previewSha256"] == digest
    # The master's digest still comes from its capture record.
    assert out["sha256"] == _sha256(Path(img.file_path).read_bytes())


@pytest.mark.unit
def test_every_problem_is_reported_at_once(client, vol):
    a = vol.record(status="captured", sequence=1)
    b = vol.record(sequence=1, upload=False)
    problems = _refusal(_export(client, vol.col))
    assert {"records_captured", "sequence_repeated", "not_uploaded"} <= set(problems)


@pytest.mark.unit
def test_a_refusal_writes_no_export_log(client, vol, db_session):
    from app.models.system_log import SystemLog
    vol.record(status="captured")
    _refusal(_export(client, vol.col))
    assert db_session.query(SystemLog).filter(SystemLog.action == "capture_manifest_exported").count() == 0


# ---------------------------------------------------------------------------
# End to end: a volume like the Rionegro staging reference, checked against
# every rule Fisqua's registration applies
# ---------------------------------------------------------------------------

def fisqua_registration_problems(doc, slug):
    """The checks of fisqua-dev scripts/lib/capture-registration.ts, in Python.

    manifestSchema (parseCaptureManifest), checkTransferRoot, planVolumes'
    not_handed_over, and planPages, without --allow-captured. Returns the
    problems Fisqua would report; an empty list means the volume registers
    (description lookups and the bucket listing aside).
    """
    out = []
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    page_key_re = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
    if doc.get("contract") != "fisqua-captua-exchange":
        out.append("contract")
    if doc.get("version") != "1.1":
        out.append("version")
    if doc.get("kind") != "capture-manifest":
        out.append("kind")
    if not isinstance(doc.get("id"), str) or not uuid_re.match(doc["id"]):
        out.append("id")
    if not isinstance(doc.get("generatedAt"), str):
        out.append("generatedAt")
    gb = doc.get("generatedBy") or {}
    if not all(isinstance(gb.get(k), str) for k in ("system", "instance", "systemVersion")):
        out.append("generatedBy")
    if "inReplyTo" not in doc or not (doc["inReplyTo"] is None or isinstance(doc["inReplyTo"], str)):
        out.append("inReplyTo")
    proj = doc.get("project") or {}
    if not (isinstance(proj.get("captuaProjectId"), int) and isinstance(proj.get("name"), str)):
        out.append("project")
    tr = doc.get("transfer") or {}
    if tr.get("channel") not in ("upload", "disk") or not tr.get("root"):
        out.append("transfer")
    if tr.get("root") != f"{slug}/":
        out.append("transfer.root")
    root = tr.get("root", "")
    stems, codes = set(), set()
    for v in doc["volumes"]:
        if v["stem"] in stems:
            out.append("stem twice")
        stems.add(v["stem"])
        if v["externalId"] is not None:
            if v["externalId"] in codes:
                out.append("externalId twice")
            codes.add(v["externalId"])
        if not (v["externalId"] is None or (isinstance(v["externalId"], str) and v["externalId"])):
            out.append("externalId")
        if not isinstance(v["captuaCollectionId"], int) or not v["stem"] or not isinstance(v["title"], str):
            out.append("volume shape")
        for r in v["records"]:
            if not isinstance(r["captuaRecordId"], int) or not isinstance(r["sequence"], int):
                out.append("record shape")
            if r["status"] not in ("captured", "in_review", "approved", "rejected"):
                out.append("status")
            if not (r["rejectionNote"] is None or isinstance(r["rejectionNote"], str)):
                out.append("rejectionNote")
            for i in r["images"]:
                if not i.get("captureId") or not ("pairId" in i and (i["pairId"] is None or isinstance(i["pairId"], str))):
                    out.append("image ids")
                if not i.get("filename") or i.get("role") not in ("left", "right", "single") or not i.get("objectKey"):
                    out.append("image shape")
                if not SHA256.match(i.get("sha256", "")):
                    out.append("sha256")
                if ("previewObjectKey" in i) != ("previewSha256" in i):
                    out.append("preview pair")
                if "previewSha256" in i and not SHA256.match(i["previewSha256"]):
                    out.append("previewSha256")
                if "previewObjectKey" in i and not i["previewObjectKey"]:
                    out.append("previewObjectKey")
        if v["externalId"] is None:
            out.append("unmatched")
            continue
        if any(r["status"] == "captured" for r in v["records"]):
            out.append("not_handed_over")
            continue
        prefix = f"{slug}/{v['stem']}/"
        file_form = re.compile(rf"^{re.escape(v['stem'])}-img_\d{{4,}}_c\d+\.[A-Za-z0-9]+$")
        if not page_key_re.match(v["stem"]):
            out.append("stem pattern")
        kept = sorted([r for r in v["records"] if r["status"] in ("in_review", "approved")],
                      key=lambda r: r["sequence"])
        seqs = [r["sequence"] for r in kept]
        if len(seqs) != len(set(seqs)):
            out.append("sequence twice")
        pages, keys = [], set()
        for r in kept:
            roles = ",".join(sorted(i["role"] for i in r["images"]))
            if roles not in ("single", "left", "right", "left,right"):
                out.append(f"roles of {r['captuaRecordId']}")
                continue
            for i in sorted(r["images"], key=lambda i: {"left": 0, "right": 1, "single": 2}[i["role"]]):
                if not file_form.match(i["filename"]):
                    out.append(f"filename {i['filename']}")
                    continue
                key = re.sub(r"\.[^.]+$", "", i["filename"])
                if not page_key_re.match(key):
                    out.append("page key pattern")
                    continue
                if key in keys:
                    out.append(f"page key {key} twice")
                    continue
                keys.add(key)
                for k in [i["objectKey"], i.get("previewObjectKey")]:
                    if k is not None and not (f"{root}{k}".startswith(prefix) and len(f"{root}{k}") > len(prefix)):
                        out.append(f"{k} outside {prefix}")
                pages.append(key)
        if not pages:
            out.append("no pages")
    return out


@pytest.mark.unit
def test_end_to_end_a_volume_like_the_staging_reference(client, vol, tmp_path):
    from app.models.record import Record

    # Openings of a RAW dual rig with previews, approved and under review.
    spreads = [vol.record(status="approved", layout=("left", "right"), ext="cr2") for _ in range(4)]
    spreads += [vol.record(status="in_review", layout=("left", "right"), ext="cr2") for _ in range(2)]
    # A cover shot alone, and a JPEG single.
    cover = vol.record(status="approved", layout=("single",), ext="cr2")
    flat = vol.record(status="in_review", layout=("single",), ext="jpg")
    # A rejected opening, retaken and approved: its old pair stays in the bucket, unlisted.
    retaken = vol.record(status="approved", layout=("left", "right"), ext="cr2")
    old_names = [i.filename for i in retaken.images]
    vol.supersede(retaken)
    vol.capture(retaken, ("left", "right"), ext="cr2")
    # A rejected opening awaiting its retake.
    rejected = vol.record(status="rejected", layout=("left", "right"), ext="cr2",
                          rejection=("focus", "img_0012_c1 out of focus"))
    # Order by a renumber that put the cover first.
    order = [cover] + spreads + [flat, retaken, rejected]
    for n, rec in enumerate(order, start=1):
        rec.sequence = n
    vol.db.commit()

    resp = _export(client, vol.col)
    assert resp.status_code == 200, resp.text
    doc = resp.json()
    (tmp_path / "manifest.json").write_text(json.dumps(doc, indent=2))

    assert fisqua_registration_problems(doc, TENANT) == []

    records = doc["volumes"][0]["records"]
    assert [r["captuaRecordId"] for r in records] == [r.id for r in order]
    images = _images(doc)
    assert len(images) == 2 * 6 + 1 + 1 + 2 + 2
    assert not set(old_names) & {i["filename"] for i in images}
    for i in images:
        assert i["objectKey"] == f"{STEM}/captures/{i['filename']}"
        if i["filename"].endswith(".cr2"):
            base = i["filename"][: -len(".cr2")]
            assert i["previewObjectKey"] == f"{STEM}/previews/{base}_preview.jpg"
        else:
            assert "previewObjectKey" not in i
    # Each digest is the one in the capture manifest for that file.
    recorded = {}
    for line in (vol.root / "metadata" / "manifest.jsonl").read_text().splitlines():
        for f in json.loads(line)["files"]:
            recorded[Path(f["relative_path"]).name] = f["sha256"]
    for i in images:
        assert i["sha256"] == recorded[i["filename"]]
        if "previewObjectKey" in i:
            assert i["previewSha256"] == recorded[i["previewObjectKey"].rsplit("/", 1)[1]]
    assert records[-1]["status"] == "rejected"
    assert records[-1]["rejectionNote"] == "img_0012_c1 out of focus"


@pytest.mark.unit
def test_the_fisqua_checks_here_catch_what_they_should(client, vol):
    """The Python port of Fisqua's checks is itself checked, on manifests it must refuse."""
    vol.record(layout=("left", "right"), ext="cr2")
    doc = _export(client, vol.col).json()
    assert fisqua_registration_problems(doc, TENANT) == []
    assert "transfer.root" in fisqua_registration_problems(doc, "other")
    bad = json.loads(json.dumps(doc))
    bad["volumes"][0]["records"][0]["status"] = "captured"
    assert "not_handed_over" in fisqua_registration_problems(bad, TENANT)
    bad = json.loads(json.dumps(doc))
    bad["volumes"][0]["records"][0]["images"][0]["filename"] = "x.cr2"
    assert any(p.startswith("filename") for p in fisqua_registration_problems(bad, TENANT))
    bad = json.loads(json.dumps(doc))
    del bad["volumes"][0]["records"][0]["images"][0]["previewSha256"]
    assert "preview pair" in fisqua_registration_problems(bad, TENANT)
    bad = json.loads(json.dumps(doc))
    bad["volumes"][0]["records"][0]["images"][1]["role"] = "left"
    assert any(p.startswith("roles") for p in fisqua_registration_problems(bad, TENANT))
    bad = json.loads(json.dumps(doc))
    bad["volumes"][0]["records"][0]["images"][0]["objectKey"] = "other/captures/x.cr2"
    assert any("outside" in p for p in fisqua_registration_problems(bad, TENANT))


# ---------------------------------------------------------------------------
# RAW, as Fisqua decides it
# ---------------------------------------------------------------------------

# Fisqua's RAW_EXTENSIONS (fisqua app/lib/capture/manifest.ts), which decide
# from a capture's filename whether its page needs a preview.
FISQUA_RAW_EXTENSIONS = (
    "3fr", "arw", "cr2", "cr3", "crw", "dcr", "dng", "erf", "iiq", "kdc", "mef", "mos", "mrw",
    "nef", "nrw", "orf", "pef", "raf", "raw", "rw2", "rwl", "sr2", "srf", "srw", "x3f",
)


@pytest.mark.unit
def test_the_unit_knows_the_raw_suffixes_fisqua_knows():
    from app.core.paths import RAW_SUFFIXES
    assert RAW_SUFFIXES == frozenset(f".{ext}" for ext in FISQUA_RAW_EXTENSIONS)


@pytest.mark.unit
def test_a_jpeg_on_disk_whose_offsite_key_is_raw_needs_a_preview(client, vol):
    """An adopted object is matched by SHA1, so its key's name can be RAW while the file on disk is not."""
    vol.record()
    rec = vol.record(layout=("single",), upload=False)
    [img] = rec.images
    data = f"{img.filename} bytes".encode()
    raw_name = f"{Path(img.filename).stem}.cr3"
    vol.offsite(img, "capture", f"{vol.tenant}/{vol.stem}/captures/{raw_name}", data)
    problems = _refusal(_export(client, vol.col))
    assert problems["raw_without_preview"]["image_ids"] == [img.id]


@pytest.mark.unit
@pytest.mark.parametrize("ext", ["cr3", "orf", "rw2"])
def test_a_raw_master_of_another_make_without_a_preview_is_refused(client, vol, ext):
    vol.record()
    rec = vol.record(layout=("single",), ext=ext, preview_digest_recorded=False)
    [img] = rec.images
    vol.db.delete(vol.row(img, "preview"))
    vol.db.commit()
    (vol.root / "previews" / f"{Path(img.filename).stem}_preview.jpg").unlink()
    problems = _refusal(_export(client, vol.col))
    assert problems["raw_without_preview"]["image_ids"] == [img.id]
