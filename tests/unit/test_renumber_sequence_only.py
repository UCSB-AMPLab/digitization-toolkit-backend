"""POST /collections/{id}/images/renumber rewrites records' `sequence` and touches no file.

Contract 1.1 §3: "Renumbering rewrites records' `sequence`; it never renames a
file." A file's name carries its capture ordinal, and order is the records'
`sequence`, which Fisqua reads to order pages and in which its registration
refuses repeats. So after a renumber the records of a volume carry 1..N, in
the order they were listed in, and every file under the projects root keeps
its name, inode, mtime and bytes; no manifest line is written.

Files are real, under the per-test projects root.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import _tree, capture_settings  # noqa: F401


def _as_operator(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x", role="operator", is_active=True)
    return client


def _volume(db, name="Tomo 1", stem="xx-test-tomo-1", project=None):
    from app.models.collection import Collection
    from app.models.project import Project

    if project is None:
        project = Project(name="P")
        db.add(project)
        db.commit()
    c = Collection(name=name, project_id=project.id, filename_stem=stem)
    db.add(c)
    db.commit()
    return project, c


def _record(db, collection_id, sequence, files=(), capture_id=None, root=None, stem=None):
    """A record with one image per (name, bytes) in `files`, written under `<root>/<stem>/captures/`."""
    from app.models.record import Record, RecordImage

    rec = Record(title=f"r{sequence}", collection_id=collection_id, sequence=sequence,
                 status="captured", capture_mode="dual" if len(files) == 2 else "single")
    db.add(rec)
    db.commit()
    for role, (name, data) in zip(("left", "right") if len(files) == 2 else ("single",), files):
        path = root / stem / "captures" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        db.add(RecordImage(record_id=rec.id, filename=name, file_path=str(path), role=role,
                           format=path.suffix.lstrip(".").lower(), capture_id=capture_id))
        if capture_id:
            meta = root / stem / "metadata"
            meta.mkdir(parents=True, exist_ok=True)
            with open(meta / "manifest.jsonl", "a") as m:
                m.write(json.dumps({"capture_id": capture_id, "files": [
                    {"relative_path": f"captures/{name}", "sha256": hashlib.sha256(data).hexdigest()}]}) + "\n")
    db.commit()
    return rec


def _sequences(db, collection_id) -> dict:
    from app.models.record import Record

    db.expire_all()
    return {r.id: r.sequence for r in db.query(Record).filter(Record.collection_id == collection_id)}


@pytest.mark.unit
def test_a_renumber_changes_sequences_and_no_file_and_writes_no_manifest_line(
        client, db_session, capture_settings, override_projects_root):
    from app.models.record import RecordImage

    root = override_projects_root
    stem = "xx-test-tomo-1"
    _p, vol = _volume(db_session, stem=stem)
    r1 = _record(db_session, vol.id, 7, [("xx-test-tomo-1-img_0003_c0.cr2", b"raw 3")], "cap-3", root, stem)
    (root / stem / "previews").mkdir(parents=True, exist_ok=True)
    (root / stem / "previews" / "xx-test-tomo-1-img_0003_c0_preview.jpg").write_bytes(b"preview 3")
    r2 = _record(db_session, vol.id, 2, [("xx-test-tomo-1-img_0001_c0.jpg", b"left 1"),
                                         ("xx-test-tomo-1-img_0001_c1.jpg", b"right 1")], "cap-1", root, stem)
    (root / stem / "thumbnails").mkdir(parents=True, exist_ok=True)
    (root / stem / "thumbnails" / ("a" * 32 + "_thumb.jpg")).write_bytes(b"thumb")
    rows_before = sorted((i.id, i.filename, i.file_path, i.thumbnail_path, i.sequence)
                         for i in db_session.query(RecordImage).all())
    files_before = _tree(root)
    manifest_before = (root / stem / "metadata" / "manifest.jsonl").read_bytes()

    resp = _as_operator(client).post(f"/collections/{vol.id}/images/renumber")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"renumbered": 2}
    assert _sequences(db_session, vol.id) == {r2.id: 1, r1.id: 2}
    assert _tree(root) == files_before
    assert (root / stem / "metadata" / "manifest.jsonl").read_bytes() == manifest_before
    assert sorted((i.id, i.filename, i.file_path, i.thumbnail_path, i.sequence)
                  for i in db_session.query(RecordImage).all()) == rows_before


@pytest.mark.unit
def test_a_renumber_leaves_one_to_n_without_repeats_in_the_listed_order(client, db_session):
    """Gaps, repeats and NULLs: order by sequence (NULLs last), then id; another volume is not touched."""
    from app.models.record import Record

    project, vol = _volume(db_session)
    _p, other = _volume(db_session, name="Tomo 2", stem="xx-test-tomo-2", project=project)
    a = _record(db_session, vol.id, 10)
    b = _record(db_session, vol.id, 3)
    c = _record(db_session, vol.id, 3)
    d = _record(db_session, vol.id, None)
    e = _record(db_session, vol.id, 0)
    f = _record(db_session, vol.id, 10)
    o1 = _record(db_session, other.id, 3)
    o2 = _record(db_session, other.id, 3)
    loose = Record(title="no volume", sequence=3, status="captured", capture_mode="single",
                   project_id=project.id)
    db_session.add(loose)
    db_session.commit()
    listed = _as_operator(client).get("/records/", params={"collection_id": vol.id})
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()] == [e.id, b.id, c.id, a.id, f.id, d.id]

    resp = _as_operator(client).post(f"/collections/{vol.id}/images/renumber")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"renumbered": 6}
    assert _sequences(db_session, vol.id) == {e.id: 1, b.id: 2, c.id: 3, a.id: 4, f.id: 5, d.id: 6}
    assert _sequences(db_session, other.id) == {o1.id: 3, o2.id: 3}
    assert db_session.get(Record, loose.id).sequence == 3

    again = _as_operator(client).post(f"/collections/{vol.id}/images/renumber")
    assert again.json() == {"renumbered": 6}
    assert _sequences(db_session, vol.id) == {e.id: 1, b.id: 2, c.id: 3, a.id: 4, f.id: 5, d.id: 6}


@pytest.mark.unit
def test_a_renumber_of_an_empty_volume_renumbers_nothing(client, db_session):
    _p, vol = _volume(db_session)
    resp = _as_operator(client).post(f"/collections/{vol.id}/images/renumber")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"renumbered": 0}


@pytest.mark.unit
def test_a_renumber_of_a_volume_that_does_not_exist_is_404(client, db_session):
    resp = _as_operator(client).post("/collections/9999/images/renumber")
    assert resp.status_code == 404, resp.text


@pytest.mark.unit
def test_a_renumber_whose_commit_fails_leaves_every_sequence_as_it_was(client, db_session, monkeypatch):
    from sqlalchemy.exc import OperationalError

    _p, vol = _volume(db_session)
    a = _record(db_session, vol.id, 5)
    b = _record(db_session, vol.id, 5)
    real_commit = db_session.commit

    def failing_commit():
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(db_session, "commit", failing_commit)
    resp = _as_operator(client).post(f"/collections/{vol.id}/images/renumber")
    monkeypatch.setattr(db_session, "commit", real_commit)

    assert resp.status_code == 500, resp.text
    assert "no record was renumbered" in resp.json()["detail"]
    assert _sequences(db_session, vol.id) == {a.id: 5, b.id: 5}


@pytest.mark.unit
def test_a_renumber_during_an_upload_run_moves_no_file_the_run_has_listed(
        client, db_session, capture_settings, override_projects_root):
    """The run has listed each image's path and opened the first; after a renumber each path still holds those bytes."""
    from app.models.record import RecordImage

    root = override_projects_root
    stem = "xx-test-tomo-1"
    _p, vol = _volume(db_session, stem=stem)
    for n, seq in ((1, 4), (2, 1), (3, 4)):
        _record(db_session, vol.id, seq, [(f"{stem}-img_{n:04d}_c0.jpg", f"page {n}".encode())], f"cap-{n}", root, stem)
    listed = [(Path(i.file_path), os.stat(i.file_path).st_ino, Path(i.file_path).read_bytes())
              for i in db_session.query(RecordImage).order_by(RecordImage.id)]

    with open(listed[0][0], "rb") as reading:
        head = reading.read(2)
        resp = _as_operator(client).post(f"/collections/{vol.id}/images/renumber")
        rest = reading.read()

    assert resp.status_code == 200, resp.text
    assert head + rest == listed[0][2]
    for path, inode, data in listed:
        assert path.exists() and os.stat(path).st_ino == inode and path.read_bytes() == data
    assert sorted(_sequences(db_session, vol.id).values()) == [1, 2, 3]


@pytest.mark.unit
def test_images_an_earlier_renumber_renamed_still_resolve_after_a_renumber(
        client, db_session, capture_settings, override_projects_root):
    """The relocation records a file-renaming renumber wrote stay, and the integrity index keeps reading them."""
    from app.core.integrity import verify_images_against_manifest
    from app.models.collection import Collection
    from app.models.record import RecordImage
    from tests.unit.earlier_renumber import rename_as_an_earlier_renumber

    root = override_projects_root
    stem = "xx-test-tomo-1"
    _p, vol = _volume(db_session, stem=stem)
    _record(db_session, vol.id, 2, [("IMG_0001.jpg", b"first page")], "cap-a", root, stem)
    _record(db_session, vol.id, 1, [("IMG_0002.jpg", b"second page")], "cap-b", root, stem)
    renamed = rename_as_an_earlier_renumber(db_session, db_session.get(Collection, vol.id))
    assert all(old.name != new.name for _, old, new in renamed)
    manifest = root / stem / "metadata" / "manifest.jsonl"
    before = manifest.read_bytes()
    assert sum(1 for l in before.decode().splitlines() if '"relocation"' in l) == 2

    resp = _as_operator(client).post(f"/collections/{vol.id}/images/renumber")

    assert resp.status_code == 200, resp.text
    assert manifest.read_bytes() == before
    db_session.expire_all()
    imgs = db_session.query(RecordImage).order_by(RecordImage.id).all()
    assert {Path(i.file_path) for i in imgs} == {new for _, _, new in renamed}
    assert verify_images_against_manifest(imgs) == []
    Path(imgs[0].file_path).write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest(imgs)] == [imgs[0].id]
