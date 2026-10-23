"""Record and image deletes unlink files only after the commit that removes the rows (NEH-262).

Both delete endpoints used to unlink the master, its `_preview.jpg` sidecar and
the thumbnail before `db.commit()`. A commit that failed then left rows
pointing at masters that were already gone from the card, which nothing can
repair. The handlers now collect the paths under the record's row lock, commit,
and unlink afterwards. A file that cannot be unlinked after a successful commit
is logged at WARNING, left on disk and named in the response's `not_removed`
list (NEH-263): the row is gone, so what remains is an extra file, which can be
found and removed later. A response with nothing left behind has no
`not_removed` key, which the exact-body assertions below check.

The files are real, under the per-test projects root from the
`override_projects_root` fixture, as in test_record_delete_sidecar.py. The
ordering is asserted with a log of events: the session's commit and every
`Path.unlink` on a file under the capture tree append to it, and every unlink
has to come after the commit.
"""

import logging
import pathlib

import pytest
from sqlalchemy.exc import OperationalError


def _user(username, role):
    from app.models.user import User

    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(username, role):
    """A client that turns an unhandled server error into a 500 instead of raising it.

    The `client` fixture's TestClient re-raises server exceptions, and a failed
    commit is one. The dependency overrides live on the app, so this client
    sees the same database session the `client` fixture installed.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    import app.api.records as records

    user = _user(username, role)
    for dep in (records.allow_contributor, records.allow_read_only):
        app.dependency_overrides[dep] = lambda u=user: u
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def capture_root(override_projects_root):
    return override_projects_root / "order_project"


@pytest.fixture
def capture_dir(capture_root):
    d = capture_root / "images" / "main"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def thumbs_dir(capture_root):
    d = capture_root / "images" / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _must_be_inside_storage(*paths):
    from app.core.paths import resolve_within_storage

    for p in paths:
        assert resolve_within_storage(str(p)) is not None, (
            f"test fixture {p} is not inside the configured storage roots; "
            f"refusing to point a delete at it"
        )


def _capture(capture_dir, thumbs_dir, stem, suffix=".CR2"):
    master = capture_dir / f"{stem}{suffix}"
    master.write_bytes(b"master-bytes")
    preview = capture_dir / f"{stem}_preview.jpg"
    preview.write_bytes(b"preview-bytes")
    thumb = thumbs_dir / f"{stem}_thumb.jpg"
    thumb.write_bytes(b"thumb-bytes")
    return master, preview, thumb


def _record(db_session, status="rejected"):
    from app.models.record import Record

    rec = Record(title="r", status=status, capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    return rec


def _image(db_session, rec, master, thumb, is_current=True):
    from app.models.record import RecordImage

    img = RecordImage(
        record_id=rec.id,
        filename=master.name,
        file_path=str(master),
        thumbnail_path=str(thumb),
        format=master.suffix.lstrip(".").lower(),
        is_current=is_current,
    )
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)
    return img


def _fail_commit(monkeypatch, db_session, files):
    """Make the next commit raise, after checking every file is still on disk."""
    seen = {}

    def failing_commit():
        seen["present"] = {p: p.exists() for p in files}
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(db_session, "commit", failing_commit)
    return seen


def _record_events(monkeypatch, db_session, capture_root):
    """Log the session's commits and every unlink under capture_root, in order."""
    events = []
    real_commit = db_session.commit
    real_unlink = pathlib.Path.unlink

    def commit():
        events.append(("commit", None))
        return real_commit()

    def unlink(self, *args, **kwargs):
        if capture_root in self.parents:
            events.append(("unlink", self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    return events


def _assert_unlinks_after_commit(events):
    kinds = [k for k, _ in events]
    assert "commit" in kinds, f"no commit recorded: {events}"
    assert "unlink" in kinds, f"no unlink recorded: {events}"
    first_commit = kinds.index("commit")
    unlinked_before = [p for k, p in events[:first_commit] if k == "unlink"]
    assert not unlinked_before, f"unlinked before the commit: {unlinked_before}"


def _fail_unlink_of(monkeypatch, target):
    real_unlink = pathlib.Path.unlink

    def unlink(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)


# ---------------------------------------------------------------------------
# DELETE /records/{rec_id}
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_record_delete_whose_commit_fails_leaves_files_and_rows(client, db_session, monkeypatch, capture_dir, thumbs_dir):
    from app.models.record import Record, RecordImage

    left = _capture(capture_dir, thumbs_dir, "IMG_1001_L")
    right = _capture(capture_dir, thumbs_dir, "IMG_1001_R")
    files = [*left, *right]
    _must_be_inside_storage(*files)
    rec = _record(db_session)
    _image(db_session, rec, left[0], left[2])
    _image(db_session, rec, right[0], right[2])
    rec_id = rec.id

    seen = _fail_commit(monkeypatch, db_session, files)
    resp = _client_as("op", "operator").delete(f"/records/{rec_id}")

    assert resp.status_code == 500, resp.text
    assert all(seen["present"].values()), f"unlinked before the commit: {seen['present']}"
    for p in files:
        assert p.exists(), f"{p.name} was unlinked although the commit failed"
    db_session.rollback()
    assert db_session.query(Record).filter(Record.id == rec_id).count() == 1
    assert db_session.query(RecordImage).filter(RecordImage.record_id == rec_id).count() == 2


@pytest.mark.unit
def test_record_delete_unlinks_every_file_after_the_commit(client, db_session, monkeypatch, capture_root, capture_dir, thumbs_dir):
    from app.models.record import Record

    left = _capture(capture_dir, thumbs_dir, "IMG_1002_L")
    right = _capture(capture_dir, thumbs_dir, "IMG_1002_R")
    files = [*left, *right]
    _must_be_inside_storage(*files)
    rec = _record(db_session)
    _image(db_session, rec, left[0], left[2])
    _image(db_session, rec, right[0], right[2])
    rec_id = rec.id

    events = _record_events(monkeypatch, db_session, capture_root)
    resp = _client_as("op", "operator").delete(f"/records/{rec_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": f"Record {rec_id} and 2 images deleted"}
    _assert_unlinks_after_commit(events)
    for p in files:
        assert not p.exists(), f"{p.name} survived the record delete"
    assert db_session.query(Record).filter(Record.id == rec_id).count() == 0


@pytest.mark.unit
def test_record_delete_logs_and_succeeds_when_an_unlink_fails_after_commit(client, db_session, monkeypatch, caplog, capture_dir, thumbs_dir):
    from app.models.record import Record, RecordImage

    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_1003")
    _must_be_inside_storage(master, preview, thumb)
    rec = _record(db_session)
    _image(db_session, rec, master, thumb)
    rec_id = rec.id

    _fail_unlink_of(monkeypatch, master)
    with caplog.at_level(logging.WARNING, logger="app.api.records"):
        resp = _client_as("op", "operator").delete(f"/records/{rec_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": f"Record {rec_id} and 1 images deleted", "not_removed": [master.name]}
    assert db_session.query(Record).filter(Record.id == rec_id).count() == 0
    assert db_session.query(RecordImage).filter(RecordImage.record_id == rec_id).count() == 0
    assert master.exists(), "the master the patched unlink refused is gone"
    assert not preview.exists(), "one failed unlink stopped the sidecar's"
    assert not thumb.exists(), "one failed unlink stopped the thumbnail's"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and str(master) in r.getMessage()]
    assert warnings, f"no WARNING naming {master}: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# DELETE /records/images/{img_id}
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_image_delete_whose_commit_fails_leaves_files_row_and_status(client, db_session, monkeypatch, capture_dir, thumbs_dir):
    """The approved -> in_review fallback is in the same transaction, so it is not kept either."""
    from app.models.record import Record, RecordImage, RecordStatusChange

    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_1004")
    files = [master, preview, thumb]
    _must_be_inside_storage(*files)
    rec = _record(db_session, status="approved")
    img = _image(db_session, rec, master, thumb)
    rec_id, img_id = rec.id, img.id

    seen = _fail_commit(monkeypatch, db_session, files)
    resp = _client_as("adm", "admin").delete(f"/records/images/{img_id}")

    assert resp.status_code == 500, resp.text
    assert all(seen["present"].values()), f"unlinked before the commit: {seen['present']}"
    for p in files:
        assert p.exists(), f"{p.name} was unlinked although the commit failed"
    db_session.rollback()
    assert db_session.query(RecordImage).filter(RecordImage.id == img_id).count() == 1
    assert db_session.query(Record).filter(Record.id == rec_id).one().status == "approved"
    assert db_session.query(RecordStatusChange).filter(
        RecordStatusChange.record_id == rec_id, RecordStatusChange.to_status == "in_review"
    ).count() == 0


@pytest.mark.unit
def test_image_delete_unlinks_after_the_commit(client, db_session, monkeypatch, capture_root, capture_dir, thumbs_dir):
    from app.models.record import RecordImage

    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_1005")
    _must_be_inside_storage(master, preview, thumb)
    rec = _record(db_session)
    img = _image(db_session, rec, master, thumb)
    img_id = img.id

    events = _record_events(monkeypatch, db_session, capture_root)
    resp = _client_as("op", "operator").delete(f"/records/images/{img_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "Image deleted"}
    _assert_unlinks_after_commit(events)
    unlinked = {p for k, p in events if k == "unlink"}
    assert {master, preview, thumb} <= unlinked, f"not every file was unlinked: {unlinked}"
    for p in (master, preview, thumb):
        assert not p.exists(), f"{p.name} survived the image delete"
    assert db_session.query(RecordImage).filter(RecordImage.id == img_id).count() == 0


@pytest.mark.unit
def test_image_delete_that_empties_an_approved_record_still_moves_it_back(client, db_session, monkeypatch, capture_root, capture_dir, thumbs_dir):
    """The fallback and its history row survive the reordering, and the files still go after the commit."""
    from app.models.record import Record, RecordStatusChange

    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_1006")
    _must_be_inside_storage(master, preview, thumb)
    rec = _record(db_session, status="approved")
    img = _image(db_session, rec, master, thumb)
    rec_id, img_id = rec.id, img.id

    events = _record_events(monkeypatch, db_session, capture_root)
    resp = _client_as("adm", "admin").delete(f"/records/images/{img_id}")

    assert resp.status_code == 200, resp.text
    _assert_unlinks_after_commit(events)
    db_session.expire_all()
    assert db_session.query(Record).filter(Record.id == rec_id).one().status == "in_review"
    change = db_session.query(RecordStatusChange).filter(
        RecordStatusChange.record_id == rec_id, RecordStatusChange.to_status == "in_review"
    ).one()
    assert change.from_status == "approved"
    assert change.changed_by == "adm"
    assert not master.exists()


@pytest.mark.unit
def test_image_delete_logs_and_succeeds_when_an_unlink_fails_after_commit(client, db_session, monkeypatch, caplog, capture_dir, thumbs_dir):
    from app.models.record import RecordImage

    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_1007")
    _must_be_inside_storage(master, preview, thumb)
    rec = _record(db_session)
    img = _image(db_session, rec, master, thumb)
    img_id = img.id

    _fail_unlink_of(monkeypatch, master)
    with caplog.at_level(logging.WARNING, logger="app.api.records"):
        resp = _client_as("op", "operator").delete(f"/records/images/{img_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detail": "Image deleted", "not_removed": [master.name]}
    assert db_session.query(RecordImage).filter(RecordImage.id == img_id).count() == 0
    assert master.exists(), "the master the patched unlink refused is gone"
    assert not preview.exists(), "one failed unlink stopped the sidecar's"
    assert not thumb.exists(), "one failed unlink stopped the thumbnail's"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and str(master) in r.getMessage()]
    assert warnings, f"no WARNING naming {master}: {[r.getMessage() for r in caplog.records]}"


@pytest.mark.unit
def test_image_delete_keeps_a_sidecar_a_surviving_sibling_shares(client, db_session, monkeypatch, capture_root, capture_dir, thumbs_dir):
    """`folio.jpg` and `folio.CR2` share `folio_preview.jpg`; deleting one keeps it, and it is never unlinked."""
    jpeg = capture_dir / "IMG_1008.jpg"
    jpeg.write_bytes(b"jpeg-master-bytes")
    raw = capture_dir / "IMG_1008.CR2"
    raw.write_bytes(b"raw-master-bytes")
    preview = capture_dir / "IMG_1008_preview.jpg"
    preview.write_bytes(b"preview-bytes")
    jpeg_thumb = thumbs_dir / "IMG_1008_jpg_thumb.jpg"
    jpeg_thumb.write_bytes(b"thumb-bytes")
    raw_thumb = thumbs_dir / "IMG_1008_raw_thumb.jpg"
    raw_thumb.write_bytes(b"thumb-bytes")
    _must_be_inside_storage(jpeg, raw, preview, jpeg_thumb, raw_thumb)
    rec = _record(db_session)
    jpeg_img = _image(db_session, rec, jpeg, jpeg_thumb)
    _image(db_session, rec, raw, raw_thumb)

    events = _record_events(monkeypatch, db_session, capture_root)
    resp = _client_as("op", "operator").delete(f"/records/images/{jpeg_img.id}")

    assert resp.status_code == 200, resp.text
    _assert_unlinks_after_commit(events)
    assert preview not in {p for k, p in events if k == "unlink"}, "the shared sidecar was unlinked"
    assert preview.exists(), f"{preview.name} was deleted while {raw.name} still points at it"
    assert raw.exists() and raw_thumb.exists(), "the surviving sibling lost a file"
    assert not jpeg.exists() and not jpeg_thumb.exists()


@pytest.mark.unit
def test_a_thumbnail_path_outside_storage_is_never_unlinked(client, db_session, tmp_path, capture_dir, thumbs_dir):
    """Every unlinked path is confined to storage, the thumbnail's included."""
    master, preview, _ = _capture(capture_dir, thumbs_dir, "IMG_1010", suffix=".jpg")
    outside = tmp_path / "outside"
    outside.mkdir()
    stray = outside / "not_ours.jpg"
    stray.write_bytes(b"someone else's file")
    rec = _record(db_session)
    img = _image(db_session, rec, master, stray)

    resp = _client_as("op", "operator").delete(f"/records/images/{img.id}")

    assert resp.status_code == 200, resp.text
    assert not master.exists()
    assert stray.exists(), "a thumbnail_path outside storage was unlinked"
