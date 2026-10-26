"""Tests of the stem layout that are not elsewhere.

The not-yet-migrated rename tests are in test_volume_not_yet_migrated, the
stray-directory test in test_stem_directory_occupied, and the torn-write tests
in test_manifest_torn_appends.

Version: 0.1.0-beta
"""

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _approve, _as_admin, _capture_records, capture_settings, data_dir, legacy, running_export,
)


class _Crash(BaseException):
    pass


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _files(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# --- a stem set while the backend runs, before the next startup -----------------

@pytest.mark.unit
def test_a_stem_set_with_captures_before_the_next_startup_then_migrated(client, db_session, legacy):
    """u gets a stem; a RAW capture lands in <stem>/captures with its record in <stem>'s manifest; then the run."""
    from app.core.export_service import _filtered_manifest_blob
    from app.core.integrity import verify_images_against_manifest
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import Record, RecordImage

    root = legacy["root"]
    i9 = db_session.get(RecordImage, legacy["i9"].id)
    i9.capture_id = "cap-i9"
    db_session.commit()
    (root / "proj" / "metadata" / "manifest.jsonl").write_text(json.dumps({"capture_id": "cap-i9", "files": [
        {"relative_path": "fondo_b/images/main/IMG_0009.CR2", "sha256": _sha(Path(i9.file_path))}]}) + "\n")
    r = _as_admin(client).patch(f"/collections/{legacy['u'].id}", json={"filename_stem": "xx-test-u"})
    assert r.status_code == 200, r.text
    vol = root / "xx-test-u"
    for d in ("captures", "previews", "thumbnails", "metadata"):
        (vol / d).mkdir(parents=True, exist_ok=True)
    new = vol / "captures" / "IMG_0010.CR2"
    new.write_bytes(b"new capture")
    (vol / "previews" / "IMG_0010_preview.jpg").write_bytes(b"new preview")
    (vol / "metadata" / "manifest.jsonl").write_text(json.dumps({"capture_id": "cap-new", "files": [
        {"relative_path": "captures/IMG_0010.CR2", "sha256": _sha(new)}]}) + "\n")
    rec = Record(title="n", collection_id=legacy["u"].id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=new.name, file_path=str(new), format="cr2",
                               role="single", capture_id="cap-new"))
    db_session.commit()

    report = migrate_volume_layout(db_session)

    db_session.expire_all()
    imgs = db_session.query(RecordImage).join(Record).filter(Record.collection_id == legacy["u"].id).all()
    assert sorted(Path(i.file_path).parent.name for i in imgs) == ["captures", "captures"]
    assert (vol / "previews" / "IMG_0009_preview.jpg").is_file() and (vol / "previews" / "IMG_0010_preview.jpg").is_file()
    assert verify_images_against_manifest(imgs) == []
    recs = db_session.query(Record).filter(Record.collection_id == legacy["u"].id).all()
    lines = [json.loads(l) for l in _filtered_manifest_blob(db_session, legacy["u"], recs).decode().splitlines()]
    print("\nBAGLINES", [(l["capture_id"], "copied_from" in l) for l in lines], "copied", report["manifest_records_copied"])
    assert sorted(l["capture_id"] for l in lines) == ["cap-i9", "cap-new"]


# --- crash safety of the manifest copy -------------------------------------------

def _crash_mid_copy(monkeypatch, db_session):
    import capture.manifestHandler as mh
    import app.core.volume_layout as vl

    real_write = mh.os.write

    def torn(fd, data):
        n = real_write(fd, bytes(data)[: max(1, len(data) // 2)])
        raise _Crash()

    monkeypatch.setattr(mh.os, "write", torn)
    with pytest.raises(_Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(mh.os, "write", real_write)


@pytest.mark.unit
def test_a_crash_mid_copy_then_the_next_run_and_a_new_capture_keep_every_record(db_session, legacy, monkeypatch):
    from app.core.integrity import build_fixity_index
    from app.core.volume_layout import migrate_volume_layout
    from capture.manifestHandler import append_manifest_record

    _capture_records(db_session, legacy)
    _crash_mid_copy(monkeypatch, db_session)
    migrate_volume_layout(db_session)
    vol = legacy["root"] / STEMS["a2"]

    class Rec:
        capture_id = "cap-after"

        def to_dict(self):
            return {"capture_id": "cap-after", "files": [{"relative_path": "captures/AFTER.jpg", "sha256": "0" * 64}]}

    append_manifest_record(vol, Rec(), "capture")
    text = (vol / "metadata" / "manifest.jsonl").read_text()
    bad = [l for l in text.splitlines() if l.strip() and not _is_json(l)]
    index = build_fixity_index(legacy["root"].resolve())
    print("\nTORN unparseable lines:", len(bad), "cap-after indexed:", "cap-after" in index.captured)
    assert "cap-after" in index.captured


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


@pytest.mark.unit
def test_a_crash_after_the_copy_adds_no_duplicate_on_the_next_run(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)
    real = vl._move

    def crash(src, dst):
        raise _Crash()

    monkeypatch.setattr(vl, "_move", crash)
    with pytest.raises(_Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(vl, "_move", real)
    vl.migrate_volume_layout(db_session)
    vl.migrate_volume_layout(db_session)
    for key in ("a1", "a2", "tomo"):
        m = legacy["root"] / STEMS[key] / "metadata" / "manifest.jsonl"
        lines = [l for l in m.read_text().splitlines() if l.strip()] if m.exists() else []
        assert len(lines) == len(set(lines)), (key, lines)


# --- the stem/project-directory collision ----------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("name", ["XX-TEST-A1", "Xx-Tëst-A1", "xx-test-a1"])
def test_a_project_named_like_a_stem_after_folding_is_refused(client, db_session, legacy, name):
    r = _as_admin(client).post("/projects/", json={"name": name})
    assert r.status_code == 409, (r.status_code, r.text)


# --- delete_project, retired manifests, copies, relocations: one checksum each ----

@pytest.mark.unit
def test_after_a_rename_clash_a_move_out_and_a_project_delete_the_bag_and_gate_count_each_capture_once(
        client, db_session, legacy):
    from app.core.export_jobs import ExportJob
    from app.core.export_service import _do_export
    from app.core.integrity import build_fixity_index, verify_images_against_manifest
    from app.core import config
    from app.core.volume_layout import migrate_volume_layout
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    _capture_records(db_session, legacy)
    cap = legacy["root"] / STEMS["a2"] / "captures"
    cap.mkdir(parents=True)
    (cap / "IMG_0002.jpg").write_bytes(b"unrelated")  # forces IMG_0002_img<id>.jpg
    migrate_volume_layout(db_session)
    (cap / "IMG_0002.jpg").unlink()

    q = Project(name="Otro")
    db_session.add(q)
    db_session.commit()
    u_files = [i.id for i in db_session.query(RecordImage).join(Record).filter(Record.collection_id == legacy["u"].id)]
    r = _as_admin(client).post(f"/projects/{legacy['proj'].id}/move-collections", json={"target_project_id": q.id})
    assert r.status_code == 200, r.text
    rec5 = db_session.get(Record, legacy["i5"].record_id)
    db_session.delete(rec5)
    db_session.commit()
    r = _as_admin(client).delete(f"/projects/{legacy['proj'].id}")
    print("\nDELETE", r.status_code, r.text[:200], sorted(p.name for p in (legacy["root"] / ".retired-manifests").glob("*")) if (legacy["root"] / ".retired-manifests").exists() else None)
    assert r.status_code in (200, 204), r.text

    db_session.expire_all()
    imgs = db_session.query(RecordImage).join(Record).filter(Record.collection_id == legacy["a2"].id).all()
    renamed = [i for i in imgs if "_img" in Path(i.file_path).name]
    assert renamed, [i.file_path for i in imgs]
    assert verify_images_against_manifest(imgs) == []
    Path(renamed[0].file_path).write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest(imgs)] == [renamed[0].id]
    Path(renamed[0].file_path).write_bytes(b"bytes of IMG_0002.jpg")

    _approve(db_session, legacy["a2"].id)
    job = ExportJob(legacy["a2"].id)
    _do_export(db_session, job, legacy["a2"].id)
    with zipfile.ZipFile(config.settings.exports_dir / job.zip_filename) as z:
        names = z.namelist()
        manifest = [json.loads(l) for l in z.read(next(n for n in names if n.endswith("data/metadata/manifest.jsonl")))
                    .decode().splitlines()]
        payload = [n for n in names if "/data/" in n and n.endswith((".jpg", ".CR2")) and "/metadata/" not in n]
    print("BAG captures", sorted(l["capture_id"] for l in manifest), "payload", len(payload))
    assert sorted(l["capture_id"] for l in manifest) == ["cap-i2", "cap-i3a", "cap-i3b"]
    assert all(l.get("record_type") != "relocation" for l in manifest)
    assert len(payload) == 3


# --- uploads ----------------------------------------------------------------------

@pytest.mark.unit
def test_an_upload_into_a_stemmed_volume_on_an_unusable_override_creates_nothing_on_the_card(
        client, db_session, legacy, monkeypatch, tmp_path):
    import io
    from PIL import Image
    from app.core import storage_override
    from app.api import records
    from app.models.record import Record

    monkeypatch.setattr(records.settings, "DATA_DIR", str(tmp_path / "data2"))
    gone = tmp_path / "unplugged" / "projects"
    monkeypatch.setattr(storage_override, "get_storage_override_or_fallback", lambda: (str(gone), False))
    rec = db_session.query(Record).filter(Record.collection_id == legacy["a1"].id).first()
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, format="JPEG")
    before = _files(legacy["root"])
    r = _as_admin(client).post(f"/records/{rec.id}/images", files={"file": ("u.jpg", buf.getvalue(), "image/jpeg")})
    print("\nUPLOAD", r.status_code, r.text[:160], "unplugged exists:", gone.exists())
    assert r.status_code == 503
    assert not gone.exists() and _files(legacy["root"]) == before


# --- mixed layout read paths -------------------------------------------------------

@pytest.mark.unit
def test_every_read_path_on_a_mixed_layout_unit(client, db_session, legacy):
    from app.core.integrity import run_integrity_check
    from app.core.volume_layout import migrate_volume_layout

    migrate_volume_layout(db_session)
    c = _as_admin(client)
    out = {}
    for key in ("i1", "i9"):  # stem RAW, name-based RAW
        iid = legacy[key].id
        out[key] = {p: c.get(f"/records/images/{iid}/{p}").status_code for p in ("file", "preview", "thumbnail")}
    result = run_integrity_check(db_session, verify_hashes=True)
    print("\nREADS", out, "missing", result["missing_files"], "orphans", result["orphan_files"])
    assert all(v == 200 for d in out.values() for v in d.values()), out
    assert result["missing_files"] == []
    assert sorted(Path(p).name for p in result["orphan_files"]) == ["stray.jpg"]


# --- scale, with a stem ----------------------------------------------------------

@pytest.mark.unit
def test_a_rionegro_sized_stemmed_volume(db_session, capture_settings, data_dir, override_projects_root):
    import time
    import tracemalloc
    from sqlalchemy import event
    import app.core.volume_layout as vl
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    p = Project(name="Rionegro")
    db_session.add(p)
    db_session.commit()
    c = Collection(name="Tomo 6", project_id=p.id, filename_stem="xx-test-rio6")
    db_session.add(c)
    db_session.commit()
    d = override_projects_root / "rionegro" / "tomo_6" / "images" / "main"
    d.mkdir(parents=True)
    recs = [Record(title=f"r{n}", collection_id=c.id, status="captured", capture_mode="dual") for n in range(2648)]
    db_session.add_all(recs)
    db_session.commit()
    imgs = []
    for n, r in enumerate(recs):
        for cam in (0, 1):
            f = d / f"20260101_000000_{n:04d}_c{cam}.jpg"
            f.write_bytes(b"x")
            imgs.append(RecordImage(record_id=r.id, filename=f.name, file_path=str(f), format="jpg", role="single"))
    db_session.add_all(imgs)
    db_session.commit()
    t0 = time.monotonic()
    first = vl.migrate_volume_layout(db_session)
    t_first = time.monotonic() - t0
    db_session.expire_all()
    stmts = []
    eng = db_session.get_bind()
    lis = lambda conn, cur, stmt, params, ctx, many: stmts.append(stmt.split()[0])
    event.listen(eng, "before_cursor_execute", lis)
    tracemalloc.start()
    t0 = time.monotonic()
    vl.migrate_volume_layout_at_startup(db_session)
    t_start = time.monotonic() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    event.remove(eng, "before_cursor_execute", lis)
    print(f"\nRIO moved={len(first['moved'])} first={t_first:.2f}s startup={t_start:.3f}s queries={len(stmts)} peak={peak/1e6:.2f}MB")
    assert len(first["moved"]) == 5296 and t_start < 2
