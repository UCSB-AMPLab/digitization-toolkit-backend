"""POST /collections/{id}/images/renumber rewrites records' `sequence` and touches no file.

Contract 1.1 §3: "Renumbering rewrites records' `sequence`; it never renames a
file." A file's name carries its capture ordinal, and order is the records'
`sequence`, which Fisqua reads to order pages and in which its registration
refuses repeats. So after a renumber the records of a volume carry 1..N, in
the order they were listed in, and every file under the projects root keeps
its name, inode, mtime and bytes; no manifest line is written.

Files are real, under the per-test projects root.

Version: 0.1.0-beta
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


# ---------------------------------------------------------------------------
# The other paths that set a record's sequence in a volume
# ---------------------------------------------------------------------------

@pytest.fixture
def statements(db_session):
    """The SQL statements the session's engine runs, in order, upper-cased."""
    from sqlalchemy import event

    seen: list = []
    engine = db_session.get_bind()

    def record(conn, cursor, statement, params, context, executemany):
        seen.append(" ".join(statement.split()).upper())

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


def _first(seen: list, *needles) -> int:
    return next(i for i, s in enumerate(seen) if all(n in s for n in needles))


@pytest.mark.unit
def test_a_reorder_sets_one_to_n_in_the_order_given(client, db_session):
    _p, vol = _volume(db_session)
    a, b, c = (_record(db_session, vol.id, s) for s in (0, 0, None))

    resp = _as_operator(client).patch(f"/collections/{vol.id}/records/reorder",
                                      json={"ordered_ids": [c.id, a.id, b.id]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reordered": 3}
    assert _sequences(db_session, vol.id) == {c.id: 1, a.id: 2, b.id: 3}


@pytest.mark.unit
@pytest.mark.parametrize("which", ["missing", "repeated", "foreign"])
def test_a_reorder_that_does_not_name_every_record_once_is_refused_and_writes_nothing(client, db_session, which):
    from app.core.record_sequence import REORDER_IDS_MESSAGE

    project, vol = _volume(db_session)
    _p, other = _volume(db_session, name="Tomo 2", stem="xx-test-tomo-2", project=project)
    a, b = _record(db_session, vol.id, 4), _record(db_session, vol.id, 9)
    o = _record(db_session, other.id, 1)
    ids = {"missing": [b.id], "repeated": [b.id, a.id, b.id], "foreign": [b.id, a.id, o.id]}[which]

    resp = _as_operator(client).patch(f"/collections/{vol.id}/records/reorder", json={"ordered_ids": ids})

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == REORDER_IDS_MESSAGE
    assert _sequences(db_session, vol.id) == {a.id: 4, b.id: 9}
    assert _sequences(db_session, other.id) == {o.id: 1}


@pytest.mark.unit
def test_a_reorder_of_a_volume_that_does_not_exist_is_404(client, db_session):
    resp = _as_operator(client).patch("/collections/9999/records/reorder", json={"ordered_ids": [1]})
    assert resp.status_code == 404, resp.text


@pytest.mark.unit
def test_records_moved_into_a_volume_take_the_sequences_after_its_highest_in_their_order(
        client, db_session, capture_settings, override_projects_root):
    project, src = _volume(db_session)
    _p, dst = _volume(db_session, name="Tomo 2", stem="xx-test-tomo-2", project=project)
    t1, t5 = _record(db_session, dst.id, 1), _record(db_session, dst.id, 5)
    s3, s1, snull = _record(db_session, src.id, 3), _record(db_session, src.id, 1), _record(db_session, src.id, None)

    resp = _as_operator(client).post(f"/collections/{src.id}/move-records",
                                     params={"target_collection_id": dst.id})

    assert resp.status_code == 200, resp.text
    assert _sequences(db_session, dst.id) == {t1.id: 1, t5.id: 5, s1.id: 6, s3.id: 7, snull.id: 8}
    assert _sequences(db_session, src.id) == {}


@pytest.mark.unit
def test_a_record_moved_into_another_volume_takes_the_next_sequence_there(client, db_session):
    project, a = _volume(db_session)
    _p, b = _volume(db_session, name="Tomo 2", stem="xx-test-tomo-2", project=project)
    r = _record(db_session, a.id, 1)
    b1, b2 = _record(db_session, b.id, 1), _record(db_session, b.id, 2)

    resp = _as_operator(client).patch(f"/records/{r.id}", json={"collection_id": b.id})

    assert resp.status_code == 200, resp.text
    assert resp.json()["sequence"] == 3
    assert _sequences(db_session, b.id) == {b1.id: 1, b2.id: 2, r.id: 3}

    same = _as_operator(client).patch(f"/records/{r.id}", json={"collection_id": b.id, "title": "t"})
    assert same.status_code == 200, same.text
    assert same.json()["sequence"] == 3


@pytest.mark.unit
def test_a_record_created_in_a_volume_takes_the_next_sequence_there(client, db_session):
    project, vol = _volume(db_session)
    api = _as_operator(client)

    first = api.post("/records/", json={"title": "a", "capture_mode": "single", "collection_id": vol.id,
                                        "sequence": 40})
    assert first.status_code == 200, first.text
    assert first.json()["sequence"] == 1
    _record(db_session, vol.id, 7)
    second = api.post("/records/", json={"title": "b", "capture_mode": "single", "collection_id": vol.id})
    assert second.json()["sequence"] == 8
    loose = api.post("/records/", json={"title": "c", "capture_mode": "single", "project_id": project.id})
    assert loose.status_code == 200, loose.text
    assert loose.json()["sequence"] is None


@pytest.mark.unit
def test_the_record_lock_queries_lock_the_records_only(db_session):
    from sqlalchemy.dialects import postgresql
    from app.core.record_sequence import _records_in_order
    from app.models.record import Record

    captured = []
    real_all = type(db_session.query(Record)).all

    def spy(query):
        captured.append(str(query.statement.compile(dialect=postgresql.dialect())).upper())
        return real_all(query)

    import unittest.mock as mock
    with mock.patch.object(type(db_session.query(Record)), "all", spy):
        _records_in_order(db_session, 1)
    assert captured and captured[0].rstrip().endswith("ORDER BY RECORDS.ID FOR UPDATE OF RECORDS"), captured


@pytest.mark.unit
def test_rewrites_and_moves_lock_records_in_id_order_and_assign_in_listing_order(
        client, db_session, statements, capture_settings, override_projects_root):
    """Record 1 at sequence 2 and record 2 at sequence 1: the rows are taken by id, as promotion takes them.

    Taken in sequence order, a renumber and a promotion over the same two
    rows could each hold one and wait for the other on PostgreSQL.
    """
    project, vol = _volume(db_session)
    _p, other = _volume(db_session, name="Tomo 2", stem="xx-test-tomo-2", project=project)
    r1, r2 = _record(db_session, vol.id, 2), _record(db_session, vol.id, 1)
    vid, oid, id1, id2 = vol.id, other.id, r1.id, r2.id
    assert id1 < id2
    api = _as_operator(client)

    def locking_selects():
        return [s for s in statements
                if s.startswith("SELECT RECORDS.ID") and "RECORDS.TITLE" in s and "FROM RECORDS" in s
                and "RECORDS.COLLECTION_ID =" in s]  # the row loads; the unlocked id re-read selects only ids

    for call in (lambda: api.post(f"/collections/{vid}/images/renumber"),
                 lambda: api.patch(f"/collections/{vid}/records/reorder", json={"ordered_ids": [id2, id1]}),
                 lambda: api.post(f"/collections/{vid}/move-records", params={"target_collection_id": oid})):
        statements.clear()
        resp = call()
        assert resp.status_code == 200, resp.text
        selects = locking_selects()
        assert selects, statements
        assert all(s.split(" ORDER BY ", 1)[1].split(" LIMIT ")[0] == "RECORDS.ID" for s in selects), selects
        target = oid if "move-records" in str(resp.url) else vid
        assert _sequences(db_session, target) == {id2: 1, id1: 2}


@pytest.mark.unit
def test_rewrites_lock_the_records_before_the_volume_and_additions_read_the_maximum_after_it(
        client, db_session, statements, capture_settings, override_projects_root):
    """SQLite emits no lock clause; the order of the statements is what reaches PostgreSQL with one."""
    project, vol = _volume(db_session)
    _p, other = _volume(db_session, name="Tomo 2", stem="xx-test-tomo-2", project=project)
    r = _record(db_session, vol.id, 1)
    api = _as_operator(client)
    # Read here: an expired object read inside a call would add its own SELECT.
    vid, oid, rid = vol.id, other.id, r.id

    def check(call, first_needles, then_needles):
        statements.clear()
        resp = call()
        assert resp.status_code == 200, resp.text
        assert _first(statements, *first_needles) < _first(statements, *then_needles), statements

    volume_row = ("FROM COLLECTIONS", "COLLECTIONS.ID =")
    record_rows = ("FROM RECORDS", "RECORDS.COLLECTION_ID =")
    highest = ("MAX(RECORDS.SEQUENCE)",)
    check(lambda: api.post(f"/collections/{vid}/images/renumber"), record_rows, volume_row)
    check(lambda: api.patch(f"/collections/{vid}/records/reorder", json={"ordered_ids": [rid]}),
          record_rows, volume_row)
    check(lambda: api.post("/records/", json={"title": "x", "capture_mode": "single", "collection_id": vid}),
          volume_row, highest)
    check(lambda: api.patch(f"/records/{rid}", json={"collection_id": oid}), ("FROM RECORDS", "RECORDS.ID ="),
          volume_row)
    check(lambda: api.patch(f"/records/{rid}", json={"collection_id": vid}), volume_row, highest)
    check(lambda: api.post(f"/collections/{vid}/move-records", params={"target_collection_id": oid}),
          record_rows, highest)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_captures_take_sequences_from_one_and_a_retake_keeps_its_own(
        client, db_session, test_project, monkeypatch, tmp_path, statements):
    from app.models.collection import Collection
    from app.models.record import Record
    from tests.unit.test_capture_birth_and_retake import (
        _capture, _capture_dual, _client_as, _install_capture_fakes,
    )

    _install_capture_fakes(monkeypatch, tmp_path)
    vol = Collection(name="V", project_id=test_project.id, filename_stem="co-test-v")
    db_session.add(vol)
    db_session.commit()
    vid, pname = vol.id, test_project.name
    api = _client_as(client, "op", "operator")

    statements.clear()
    assert _capture(api, pname, collection_id=vid).status_code == 200
    assert _first(statements, "FROM COLLECTIONS", "COLLECTIONS.ID =") < _first(statements, "MAX(RECORDS.SEQUENCE)")
    assert _capture_dual(api, pname, collection_id=vid).status_code == 200
    first, second = db_session.query(Record).filter(Record.collection_id == vid).order_by(Record.id).all()
    assert (first.sequence, second.sequence) == (1, 2)
    rid = first.id

    statements.clear()
    resp = _capture(api, pname, collection_id=vid, record_id=rid)
    assert resp.status_code == 200, resp.text
    assert _first(statements, "FROM RECORDS", "RECORDS.ID =") < _first(statements, "FROM COLLECTIONS", "COLLECTIONS.ID =")
    assert not any("MAX(RECORDS.SEQUENCE)" in s for s in statements)
    assert _sequences(db_session, vid) == {first.id: 1, second.id: 2}


# ---------------------------------------------------------------------------
# No record lock once the volume's row is held
# ---------------------------------------------------------------------------

@pytest.fixture
def pg_statements(db_session):
    """Each ORM statement the session runs, compiled for PostgreSQL, and "ROLLBACK" for each rollback."""
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql

    seen: list = []

    def on_execute(state):
        seen.append(" ".join(str(state.statement.compile(dialect=postgresql.dialect())).split()).upper())

    def on_rollback(session):
        seen.append("ROLLBACK")

    event.listen(db_session, "do_orm_execute", on_execute)
    event.listen(db_session, "after_rollback", on_rollback)
    yield seen
    event.remove(db_session, "do_orm_execute", on_execute)
    event.remove(db_session, "after_rollback", on_rollback)


def _assert_no_record_lock_while_the_volume_is_held(seen: list) -> int:
    """Between a volume lock and the next rollback (or the end) no records row is locked; return the attempts."""
    attempts, holding = 0, False
    for s in seen:
        if s == "ROLLBACK":
            holding = False
        elif "FROM COLLECTIONS" in s and s.endswith("FOR UPDATE"):
            holding = True
            attempts += 1
        elif "FROM RECORDS" in s and "FOR UPDATE" in s:
            assert not holding, seen
    return attempts


def _adding_a_record_on_each_volume_lock(monkeypatch, collection_id, times):
    """Before each of the first `times` volume locks, another session adds a record to the volume and commits."""
    import app.core.record_sequence as rs
    from app.core.db import SessionLocal
    from app.models.record import Record

    real = rs.locked_volume_query
    calls = {"n": 0}

    def adding(db, cid):
        calls["n"] += 1
        if calls["n"] <= times:
            other = SessionLocal()
            try:
                other.add(Record(title=f"added {calls['n']}", collection_id=collection_id, sequence=1,
                                 status="captured", capture_mode="single"))
                other.commit()
            finally:
                other.close()
        return real(db, cid)

    monkeypatch.setattr(rs, "locked_volume_query", adding)
    return calls


@pytest.mark.unit
def test_the_recheck_after_the_volume_lock_takes_no_lock(db_session):
    from sqlalchemy.dialects import postgresql
    from app.core.record_sequence import _record_ids

    sql = str(_record_ids(db_session, 1).statement.compile(dialect=postgresql.dialect())).upper()
    assert "FROM RECORDS" in sql and "FOR UPDATE" not in sql, sql


@pytest.mark.unit
def test_a_renumber_and_a_reorder_lock_no_record_after_the_volume(client, db_session, pg_statements):
    _p, vol = _volume(db_session)
    r1, r2 = _record(db_session, vol.id, 2), _record(db_session, vol.id, 1)
    vid, id1, id2 = vol.id, r1.id, r2.id
    api = _as_operator(client)

    pg_statements.clear()
    assert api.post(f"/collections/{vid}/images/renumber").status_code == 200
    assert _assert_no_record_lock_while_the_volume_is_held(pg_statements) == 1
    assert any("FROM RECORDS" in s and "FOR UPDATE" in s for s in pg_statements)

    pg_statements.clear()
    assert api.patch(f"/collections/{vid}/records/reorder", json={"ordered_ids": [id1, id2]}).status_code == 200
    assert _assert_no_record_lock_while_the_volume_is_held(pg_statements) == 1


@pytest.mark.unit
def test_a_record_added_before_the_volume_lock_makes_the_renumber_start_over_and_include_it(
        client, db_session, pg_statements, monkeypatch):
    from app.models.record import Record

    _p, vol = _volume(db_session)
    r1, r2 = _record(db_session, vol.id, 5), _record(db_session, vol.id, 3)
    vid, id1, id2 = vol.id, r1.id, r2.id
    calls = _adding_a_record_on_each_volume_lock(monkeypatch, vid, times=1)

    pg_statements.clear()
    resp = _as_operator(client).post(f"/collections/{vid}/images/renumber")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"renumbered": 3}
    assert calls["n"] == 2
    assert _assert_no_record_lock_while_the_volume_is_held(pg_statements) == 2
    added = db_session.query(Record).filter(Record.title == "added 1").one().id
    assert _sequences(db_session, vid) == {added: 1, id2: 2, id1: 3}


@pytest.mark.unit
@pytest.mark.parametrize("route", ["renumber", "reorder"])
def test_a_volume_that_keeps_changing_is_refused_with_a_409_after_five_attempts(
        client, db_session, pg_statements, monkeypatch, route):
    from app.core.record_sequence import LOCK_ATTEMPTS, VOLUME_CHANGED_MESSAGE

    _p, vol = _volume(db_session)
    r1, r2 = _record(db_session, vol.id, 5), _record(db_session, vol.id, 3)
    vid, id1, id2 = vol.id, r1.id, r2.id
    calls = _adding_a_record_on_each_volume_lock(monkeypatch, vid, times=LOCK_ATTEMPTS)
    api = _as_operator(client)

    pg_statements.clear()
    if route == "renumber":
        resp = api.post(f"/collections/{vid}/images/renumber")
    else:
        resp = api.patch(f"/collections/{vid}/records/reorder", json={"ordered_ids": [id1, id2]})

    assert LOCK_ATTEMPTS == 5
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == VOLUME_CHANGED_MESSAGE == "The volume changed while it was being renumbered. Try again."
    assert calls["n"] == LOCK_ATTEMPTS
    assert _assert_no_record_lock_while_the_volume_is_held(pg_statements) == LOCK_ATTEMPTS
    seqs = _sequences(db_session, vid)
    assert (seqs[id1], seqs[id2]) == (5, 3)
