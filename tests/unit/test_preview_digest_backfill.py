"""`python -m app.core.image_upload backfill-preview-digests <stem> [--apply]` (app.core.preview_digests).

For RAW previews captured before capture recorded their sha256: one
`preview_digest` record per preview, appended to the volume's manifest,
computed from the local file and checked against the offsite SHA1 where the
preview has been uploaded. A dry run unless `--apply`; existing lines are
never rewritten.

    python -m pytest tests/unit/test_preview_digest_backfill.py
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_preview_digests import STEM, TENANT, _append, _lines, _sha1, _sha256, _write

class Vol:
    """A stemmed volume with RAW captures recorded as before this change: no preview digests."""

    def __init__(self, db, root: Path, stem=STEM):
        from app.models.collection import Collection
        from app.models.project import Project

        self.db = db
        self.stem = stem
        self.dir = root / stem
        for sub in ("captures", "previews", "metadata"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        p = db.query(Project).filter(Project.name == "P").one_or_none()
        if p is None:
            p = Project(name="P")
            db.add(p)
            db.commit()
        self.col = Collection(name=stem, project_id=p.id, filename_stem=stem)
        db.add(self.col)
        db.commit()
        self.n = 0
        self.manifest = self.dir / "metadata" / "manifest.jsonl"

    def image(self, ext="cr2", preview=True, capture_id=True, current=True, status="approved"):
        from app.models.record import Record, RecordImage

        self.n += 1
        name = f"{self.stem}-img_{self.n:04d}_c0.{ext}"
        master = self.dir / "captures" / name
        data = _write(master, f"master {name}".encode())
        rec = Record(title=name, collection_id=self.col.id, status=status, sequence=self.n, capture_mode="single")
        self.db.add(rec)
        self.db.commit()
        cid = f"cap-{self.n}" if capture_id else None
        img = RecordImage(record_id=rec.id, capture_id=cid, role="single", filename=name,
                          file_path=str(master), format=ext, is_current=current)
        self.db.add(img)
        self.db.commit()
        if cid:
            _append(self.dir, {"capture_id": cid, "files": [
                {"role": "single", "relative_path": f"captures/{name}", "sha256": _sha256(data)}]})
        pdata = None
        if preview:
            pdata = _write(self.dir / "previews" / f"{Path(name).stem}_preview.jpg", f"preview {name}".encode())
        return img, pdata

    def preview_path(self, img) -> Path:
        return self.dir / "previews" / f"{Path(img.filename).stem}_preview.jpg"

    def offsite(self, img, data):
        from app.models.offsite_object import OffsiteObject
        now = datetime.now(timezone.utc)
        self.db.add(OffsiteObject(record_image_id=img.id, kind="preview",
                                  object_key=f"{TENANT}/{self.stem}/previews/{self.preview_path(img).name}",
                                  local_sha1=_sha1(data), uploaded_at=now, verified_at=now))
        self.db.commit()


@pytest.fixture
def vol(db_session, override_projects_root):
    return Vol(db_session, override_projects_root)


def _backfill(db, stem=STEM, apply=False):
    from app.core.preview_digests import backfill_preview_digests
    return backfill_preview_digests(db, stem, apply=apply)


@pytest.mark.unit
def test_a_dry_run_reports_and_writes_nothing(vol, db_session):
    img, data = vol.image()
    before = vol.manifest.read_bytes()
    report = _backfill(db_session)
    assert report["applied"] is False
    assert [e["image_id"] for e in report["backfilled"]] == [img.id]
    assert report["backfilled"][0]["sha256"] == _sha256(data)
    assert vol.manifest.read_bytes() == before


@pytest.mark.unit
def test_apply_appends_one_record_per_preview_and_rewrites_nothing(vol, db_session):
    current, cdata = vol.image()
    old, odata = vol.image(current=False)
    vol.image(ext="jpg", preview=False)  # not a RAW: nothing to do
    before = vol.manifest.read_bytes()

    report = _backfill(db_session, apply=True)

    after = vol.manifest.read_bytes()
    assert after.startswith(before)
    added = [json.loads(l) for l in after[len(before):].decode().splitlines()]
    assert sorted(r["image_id"] for r in added) == sorted([current.id, old.id])
    rec = next(r for r in added if r["image_id"] == current.id)
    p = vol.preview_path(current)
    assert rec["record_type"] == "preview_digest"
    assert rec["capture_id"] == current.capture_id
    assert rec["backfilled"] is True
    datetime.fromisoformat(rec["computed_at"])
    assert rec["files"] == [{"role": "single_preview", "relative_path": f"previews/{p.name}",
                             "bytes": len(cdata), "mimetype": "image/jpeg", "sha256": _sha256(cdata)}]
    assert report["applied"] is True and len(report["backfilled"]) == 2


@pytest.mark.unit
def test_after_a_backfill_the_index_holds_the_preview_digest(vol, db_session, override_projects_root):
    from app.core.integrity import build_fixity_index

    img, data = vol.image()
    _backfill(db_session, apply=True)
    index = build_fixity_index(override_projects_root.resolve())
    assert index.expected(img.capture_id, None, vol.preview_path(img).resolve()) == _sha256(data)


@pytest.mark.unit
def test_a_second_run_finds_nothing_to_do(vol, db_session):
    vol.image()
    _backfill(db_session, apply=True)
    before = vol.manifest.read_bytes()
    report = _backfill(db_session, apply=True)
    assert report["backfilled"] == [] and report["already_recorded"] == 1
    assert vol.manifest.read_bytes() == before


@pytest.mark.unit
def test_a_preview_recorded_at_capture_is_left_alone(vol, db_session):
    img, data = vol.image()
    _append(vol.dir, {"capture_id": img.capture_id, "files": [
        {"role": "single_preview", "relative_path": f"previews/{vol.preview_path(img).name}",
         "sha256": _sha256(data)}]})
    report = _backfill(db_session, apply=True)
    assert report["backfilled"] == [] and report["already_recorded"] == 1


@pytest.mark.unit
def test_an_uploaded_preview_whose_sha1_matches_is_backfilled(vol, db_session):
    img, data = vol.image()
    vol.offsite(img, data)
    report = _backfill(db_session, apply=True)
    assert [e["image_id"] for e in report["backfilled"]] == [img.id]
    assert report["backfilled"][0]["checked_against_offsite"] is True


@pytest.mark.unit
def test_an_uploaded_preview_whose_file_has_changed_is_reported_not_backfilled(vol, db_session):
    img, data = vol.image()
    vol.offsite(img, data)
    vol.preview_path(img).write_bytes(b"changed since the upload")
    before = vol.manifest.read_bytes()
    report = _backfill(db_session, apply=True)
    assert report["backfilled"] == []
    [entry] = report["sha1_differs"]
    assert entry["image_id"] == img.id
    assert entry["offsite_sha1"] == _sha1(data)
    assert entry["local_sha1"] == _sha1(b"changed since the upload")
    assert vol.manifest.read_bytes() == before


@pytest.mark.unit
def test_a_freed_preview_is_reported(vol, db_session, monkeypatch):
    import app.core.preview_digests as pd

    img, _ = vol.image()
    vol.preview_path(img).unlink()
    monkeypatch.setattr(pd, "_preview_freed", lambda image: image.id == img.id)
    report = _backfill(db_session, apply=True)
    assert [e["image_id"] for e in report["freed"]] == [img.id]
    assert report["backfilled"] == [] and report["missing"] == []


@pytest.mark.unit
def test_the_freed_guard_reads_the_column_only_where_it_exists():
    from app.core.preview_digests import _preview_freed
    assert _preview_freed(SimpleNamespace()) is False
    assert _preview_freed(SimpleNamespace(preview_freed_at=None)) is False
    assert _preview_freed(SimpleNamespace(preview_freed_at=datetime.now(timezone.utc))) is True


@pytest.mark.unit
def test_a_missing_preview_file_is_reported(vol, db_session):
    img, _ = vol.image(preview=False)
    report = _backfill(db_session, apply=True)
    assert [e["image_id"] for e in report["missing"]] == [img.id]


@pytest.mark.unit
def test_an_image_with_no_capture_id_is_reported(vol, db_session):
    img, _ = vol.image(capture_id=False)
    report = _backfill(db_session, apply=True)
    assert [e["image_id"] for e in report["no_capture_id"]] == [img.id]
    assert report["backfilled"] == []


@pytest.mark.unit
def test_only_the_named_volume_is_backfilled(vol, db_session, override_projects_root):
    other = Vol(db_session, override_projects_root, stem="co-test-other")
    other.image()
    img, _ = vol.image()
    report = _backfill(db_session, apply=True)
    assert [e["image_id"] for e in report["backfilled"]] == [img.id]
    assert len(_lines(other.manifest)) == 1


@pytest.mark.unit
def test_an_unknown_stem_is_refused(db_session):
    with pytest.raises(ValueError):
        _backfill(db_session, stem="no-such-stem")


@pytest.mark.unit
def test_apply_is_logged(vol, db_session):
    from app.models.system_log import SystemLog
    vol.image()
    _backfill(db_session, apply=True)
    row = db_session.query(SystemLog).filter(SystemLog.action == "preview_digests_backfilled").one()
    assert row.subject == STEM and "backfilled=1" in row.detail


@pytest.mark.unit
def test_the_command_line(vol, db_session, capsys):
    from app.core.image_upload import main

    img, _ = vol.image()
    before = vol.manifest.read_bytes()
    assert main(["backfill-preview-digests", STEM]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and [e["image_id"] for e in out["backfilled"]] == [img.id]
    assert vol.manifest.read_bytes() == before
    assert main(["backfill-preview-digests", STEM, "--apply"]) == 0
    assert len(_lines(vol.manifest)) == 2
    assert main(["backfill-preview-digests", "no-such-stem"]) == 2


# ---------------------------------------------------------------------------
# The row is read again before and after the hash, and only that read counts
# ---------------------------------------------------------------------------

def _change_before_the_state_read(monkeypatch, db, image_id, sql):
    """Run `sql` on the image's row just before the backfill first reads its state."""
    from sqlalchemy import text
    import app.core.image_upload as iu

    real = iu._image_state
    done = []

    def change_then_read(session, iid):
        if iid == image_id and not done:
            done.append(iid)
            db.execute(text(sql), {"id": image_id})
            db.commit()
        return real(session, iid)

    monkeypatch.setattr(iu, "_image_state", change_then_read)


@pytest.mark.unit
def test_an_image_deleted_before_its_state_is_read_is_reported(vol, db_session, monkeypatch):
    img, _ = vol.image()
    image_id = img.id
    before = vol.manifest.read_bytes()
    _change_before_the_state_read(monkeypatch, db_session, image_id, "DELETE FROM record_images WHERE id = :id")
    report = _backfill(db_session, apply=True)
    assert report["backfilled"] == []
    assert [e["image_id"] for e in report["changed"]] == [image_id]
    assert vol.manifest.read_bytes() == before


@pytest.mark.unit
def test_an_image_moved_out_before_its_state_is_read_is_reported(vol, db_session, monkeypatch, override_projects_root):
    other = Vol(db_session, override_projects_root, stem="co-test-other")
    img, _ = vol.image()
    before = vol.manifest.read_bytes()
    _change_before_the_state_read(
        monkeypatch, db_session, img.id,
        f"UPDATE records SET collection_id = {other.col.id} "
        "WHERE id = (SELECT record_id FROM record_images WHERE id = :id)")
    report = _backfill(db_session, apply=True)
    assert report["backfilled"] == []
    assert [e["image_id"] for e in report["changed"]] == [img.id]
    assert vol.manifest.read_bytes() == before


@pytest.mark.unit
def test_the_path_and_capture_id_come_from_the_fresh_row(vol, db_session, monkeypatch):
    """A row renamed before its state is read is backfilled at its new name, under its current capture id."""
    img, data = vol.image()
    new_master = vol.dir / "captures" / f"{STEM}-img_0099_c0.cr2"
    vol.preview_path(img).rename(vol.dir / "previews" / f"{STEM}-img_0099_c0_preview.jpg")
    _change_before_the_state_read(
        monkeypatch, db_session, img.id,
        f"UPDATE record_images SET file_path = '{new_master}', capture_id = 'cap-fresh' WHERE id = :id")
    report = _backfill(db_session, apply=True)
    [entry] = report["backfilled"]
    assert entry["relative_path"] == f"previews/{STEM}-img_0099_c0_preview.jpg"
    rec = _lines(vol.manifest)[-1]
    assert rec["capture_id"] == "cap-fresh"
    assert rec["files"][0]["relative_path"] == f"previews/{STEM}-img_0099_c0_preview.jpg"
    assert rec["files"][0]["sha256"] == _sha256(data)


@pytest.mark.unit
def test_an_image_changed_while_its_preview_is_hashed_is_reported(vol, db_session, monkeypatch):
    from sqlalchemy import text
    import app.core.image_upload as iu

    img, _ = vol.image()
    real = iu.stable_digests

    def hash_then_move(path):
        out = real(path)
        db_session.execute(text("UPDATE record_images SET file_path = 'elsewhere.cr2' WHERE id = :id"),
                           {"id": img.id})
        db_session.commit()
        return out

    monkeypatch.setattr(iu, "stable_digests", hash_then_move)
    report = _backfill(db_session, apply=True)
    assert report["backfilled"] == []
    assert [e["image_id"] for e in report["changed"]] == [img.id]
