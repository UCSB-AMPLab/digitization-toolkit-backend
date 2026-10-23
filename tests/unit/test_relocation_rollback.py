"""A move whose relocation or commit fails puts every moved file back.

Three routes relocate image files with `relocate_image` and commit once at
the end: POST /collections/{id}/move-records, PATCH /collections/{id} (a
re-parent under a collection of another project; a rename moves no file),
and POST /projects/{id}/move-collections. A failure
part-way through the moves, or at the commit, rolls the session back, so the
rows keep their old paths; the files already moved are moved back to match
them, RAW preview sidecars included.

Files are real, under the per-test projects root from `override_projects_root`.
"""

import hashlib
import logging
import shutil
from pathlib import Path

import pytest


def _as_operator(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x", role="operator", is_active=True)
    return client


@pytest.fixture
def capture_settings(override_projects_root, monkeypatch):
    """Point capture.project_manager at this test's projects root (see test_sidecar_follows_master)."""
    import capture.project_manager as pm
    from app.core import config

    monkeypatch.setattr(pm, "settings", config.settings)


@pytest.fixture
def layout(db_session, capture_settings):
    """Projects "proj" and "proj2"; collections A and B in proj, C in proj2; three records in A.

    Two records hold a RAW master with its `_preview.jpg` sidecar, the third a
    JPEG master. Every image has a thumbnail file in `images/thumbnails`.
    """
    from capture.project_manager import image_output_dir
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    proj = Project(name="proj")
    proj2 = Project(name="proj2")
    db_session.add_all([proj, proj2])
    db_session.commit()
    a = Collection(name="A", project_id=proj.id)
    b = Collection(name="B", project_id=proj.id)
    c = Collection(name="C", project_id=proj2.id)
    db_session.add_all([a, b, c])
    db_session.commit()

    src_dir = image_output_dir("proj", a.id)
    src_dir.mkdir(parents=True)
    thumb_dir = src_dir.parent / "thumbnails"
    thumb_dir.mkdir()
    for name in ("IMG_0001.CR2", "IMG_0002.CR2", "IMG_0003.jpg"):
        master = src_dir / name
        master.write_bytes(f"master {name}".encode())
        if master.suffix == ".CR2":
            (src_dir / f"{master.stem}_preview.jpg").write_bytes(f"preview {name}".encode())
        rec = Record(title=name, collection_id=a.id, status="captured", capture_mode="single")
        db_session.add(rec)
        db_session.commit()
        thumb = thumb_dir / f"{master.stem}_thumb.jpg"
        thumb.write_bytes(f"thumb {name}".encode())
        db_session.add(RecordImage(record_id=rec.id, filename=name, file_path=str(master),
                                   thumbnail_path=str(thumb), format=master.suffix.lstrip(".").lower()))
        db_session.commit()
    return {"proj": proj, "proj2": proj2, "a": a, "b": b, "c": c, "src_dir": src_dir, "thumb_dir": thumb_dir}


# Each route: how to call it, and the (project name, collection id) of the
# directory it moves A's images into.
ROUTES = {
    "move_records": (
        lambda api, L: api.post(f"/collections/{L['a'].id}/move-records?target_collection_id={L['b'].id}"),
        lambda L: ("proj", L["b"].id),
    ),
    "reparent_collection": (
        lambda api, L: api.patch(f"/collections/{L['a'].id}", json={"parent_collection_id": L["c"].id}),
        lambda L: ("proj2", L["a"].id),
    ),
    "move_collections": (
        lambda api, L: api.post(f"/projects/{L['proj'].id}/move-collections",
                                json={"target_project_id": L["proj2"].id}),
        lambda L: ("proj2", L["a"].id),
    ),
}


def _files(root: Path) -> dict:
    """Every file under `root`, by path relative to it, with a digest of its bytes."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _rows(db_session) -> dict:
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage

    return {
        "images": sorted((i.id, i.file_path, i.filename, i.thumbnail_path)
                         for i in db_session.query(RecordImage).all()),
        "records": sorted((r.id, r.collection_id) for r in db_session.query(Record).all()),
        "collections": sorted((c.id, c.name, c.project_id, c.parent_collection_id)
                              for c in db_session.query(Collection).all()),
    }


def _fail_nth_master_move(monkeypatch, n):
    """Make the n-th move of a master (not a `_preview.jpg`) raise; every other move runs."""
    real_move = shutil.move
    count = {"masters": 0}

    def move(src, dst, *args, **kw):
        if not str(src).endswith("_preview.jpg"):
            count["masters"] += 1
            if count["masters"] == n:
                raise PermissionError(13, "Permission denied", src)
        return real_move(src, dst, *args, **kw)

    monkeypatch.setattr("app.core.storage_ops.shutil.move", move)


def _fail_commit(monkeypatch, db_session):
    from sqlalchemy.exc import OperationalError

    def failing_commit():
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(db_session, "commit", failing_commit)


@pytest.mark.unit
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_failed_move_puts_every_moved_file_back(route, client, db_session, layout,
                                                   override_projects_root, monkeypatch):
    call, _ = ROUTES[route]
    files_before = _files(override_projects_root)
    rows_before = _rows(db_session)
    _fail_nth_master_move(monkeypatch, 2)

    resp = call(_as_operator(client), layout)

    assert resp.status_code == 500, resp.text
    assert "back where they were" in resp.json()["detail"]
    assert _files(override_projects_root) == files_before
    db_session.rollback()
    assert _rows(db_session) == rows_before


@pytest.mark.unit
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_failed_commit_puts_every_moved_file_back(route, client, db_session, layout,
                                                     override_projects_root, monkeypatch):
    call, _ = ROUTES[route]
    files_before = _files(override_projects_root)
    rows_before = _rows(db_session)
    _fail_commit(monkeypatch, db_session)

    resp = call(_as_operator(client), layout)

    assert resp.status_code == 500, resp.text
    assert "back where they were" in resp.json()["detail"]
    assert _files(override_projects_root) == files_before
    db_session.rollback()
    assert _rows(db_session) == rows_before


@pytest.mark.unit
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_move_that_succeeds_relocates_masters_and_sidecars(route, client, db_session, layout):
    from capture.project_manager import image_output_dir
    from app.models.record import RecordImage

    call, target = ROUTES[route]
    resp = call(_as_operator(client), layout)

    assert resp.status_code == 200, resp.text
    assert "not_moved" not in resp.json()
    new_dir = image_output_dir(*target(layout))
    assert sorted(p.name for p in new_dir.iterdir()) == [
        "IMG_0001.CR2", "IMG_0001_preview.jpg", "IMG_0002.CR2", "IMG_0002_preview.jpg", "IMG_0003.jpg",
    ]
    assert not any(p.is_file() for p in layout["src_dir"].iterdir())
    db_session.expire_all()
    for img in db_session.query(RecordImage).all():
        assert Path(img.file_path) == (new_dir / img.filename).resolve()
        assert Path(img.file_path).read_bytes() == f"master {img.filename}".encode()
        assert img.thumbnail_path is None
    # The old thumbnails go once the commit has landed; they regenerate on demand.
    assert list(layout["thumb_dir"].iterdir()) == []


@pytest.mark.unit
def test_a_move_back_that_fails_is_logged_and_the_original_error_still_reported(
        client, db_session, layout, override_projects_root, monkeypatch, caplog):
    from capture.project_manager import image_output_dir

    call, _ = ROUTES["move_records"]
    new_dir = image_output_dir("proj", layout["b"].id)
    real_move = shutil.move
    count = {"masters": 0}

    def move(src, dst, *args, **kw):
        if Path(src).parent == new_dir.resolve() and Path(src).suffix == ".CR2":
            raise PermissionError(13, "Permission denied", src)  # the master's move back
        if not str(src).endswith("_preview.jpg"):
            count["masters"] += 1
            if count["masters"] == 2:
                raise PermissionError(13, "Permission denied", src)
        return real_move(src, dst, *args, **kw)

    monkeypatch.setattr("app.core.storage_ops.shutil.move", move)
    with caplog.at_level(logging.ERROR, logger="app.core.storage_ops"):
        resp = call(_as_operator(client), layout)

    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert "could not be put back" in detail
    assert str(override_projects_root) not in detail and "Permission denied" not in detail
    stranded = [p.resolve() for p in new_dir.iterdir() if p.suffix == ".CR2"]
    assert len(stranded) == 1
    original = layout["src_dir"].resolve() / stranded[0].name
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any(str(stranded[0]) in m and str(original) in m for m in errors), errors
    # The sidecar moved after its master, so it went back first and sits in the old directory.
    assert (layout["src_dir"] / f"{stranded[0].stem}_preview.jpg").exists()


@pytest.mark.unit
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_failed_move_keeps_paths_and_errors_out_of_the_detail(route, client, layout,
                                                               override_projects_root, monkeypatch, caplog):
    call, _ = ROUTES[route]
    _fail_nth_master_move(monkeypatch, 1)
    with caplog.at_level(logging.ERROR, logger="app.core.storage_ops"):
        resp = call(_as_operator(client), layout)

    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert str(override_projects_root) not in detail and "/images/main/" not in detail, detail
    assert "Permission denied" not in detail
    assert any("Permission denied" in r.getMessage() or (r.exc_text and "Permission denied" in r.exc_text)
               or (r.exc_info and "Permission denied" in str(r.exc_info[1]))
               for r in caplog.records), "the error belongs in the log"


def _commit_lands_then_raises(monkeypatch, db_session):
    """The first commit applies the transaction, then reports a lost connection."""
    from sqlalchemy.exc import OperationalError

    real_commit = db_session.commit
    state = {"n": 0}

    def commit_then_raise():
        real_commit()
        state["n"] += 1
        if state["n"] == 1:
            raise OperationalError("COMMIT", {}, Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(db_session, "commit", commit_then_raise)


@pytest.mark.unit
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_commit_that_landed_before_raising_keeps_the_files_moved(route, client, db_session, layout,
                                                                    monkeypatch, caplog):
    from capture.project_manager import image_output_dir
    from app.models.record import RecordImage

    call, target = ROUTES[route]
    _commit_lands_then_raises(monkeypatch, db_session)
    with caplog.at_level(logging.WARNING, logger="app.core.storage_ops"):
        resp = call(_as_operator(client), layout)

    assert resp.status_code == 200, resp.text
    assert "not_moved" not in resp.json()
    new_dir = image_output_dir(*target(layout))
    db_session.expire_all()
    for img in db_session.query(RecordImage).all():
        assert Path(img.file_path) == (new_dir / img.filename).resolve()
        assert Path(img.file_path).exists()
    assert any(r.levelno == logging.WARNING and "landed" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_commit_whose_outcome_cannot_be_checked_leaves_the_files_moved(route, client, db_session, layout,
                                                                          monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError
    from capture.project_manager import image_output_dir

    call, target = ROUTES[route]
    real_query = db_session.query
    state = {"down": False}

    def failing_commit():
        state["down"] = True
        raise OperationalError("COMMIT", {}, Exception("server closed the connection unexpectedly"))

    def query(*args, **kw):
        if state["down"]:
            raise OperationalError("SELECT", {}, Exception("could not connect to server"))
        return real_query(*args, **kw)

    monkeypatch.setattr(db_session, "commit", failing_commit)
    monkeypatch.setattr(db_session, "query", query)
    with caplog.at_level(logging.ERROR, logger="app.core.storage_ops"):
        resp = call(_as_operator(client), layout)

    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert "could not be determined" in detail
    new_dir = image_output_dir(*target(layout))
    assert sorted(p.name for p in new_dir.iterdir()) == [
        "IMG_0001.CR2", "IMG_0001_preview.jpg", "IMG_0002.CR2", "IMG_0002_preview.jpg", "IMG_0003.jpg",
    ]
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    src = layout["src_dir"].resolve()
    for name in ("IMG_0001.CR2", "IMG_0001_preview.jpg", "IMG_0002.CR2", "IMG_0002_preview.jpg", "IMG_0003.jpg"):
        assert any(str(src / name) in m and str(new_dir.resolve() / name) in m for m in errors), name
    # Nothing was deleted while the outcome is unknown.
    assert len(list(layout["thumb_dir"].iterdir())) == 3


@pytest.mark.unit
def test_a_rollback_that_raises_does_not_skip_the_undo(client, db_session, layout,
                                                       override_projects_root, monkeypatch):
    call, _ = ROUTES["move_records"]
    files_before = _files(override_projects_root)
    _fail_nth_master_move(monkeypatch, 2)
    real_rollback = db_session.rollback

    def bad_rollback():
        raise RuntimeError("rollback failed: connection gone")

    monkeypatch.setattr(db_session, "rollback", bad_rollback)
    resp = call(_as_operator(client), layout)

    assert resp.status_code == 500, resp.text
    assert "back where they were" in resp.json()["detail"]
    assert _files(override_projects_root) == files_before
    real_rollback()


@pytest.mark.unit
def test_an_interrupt_mid_loop_puts_the_files_back_and_propagates(db_session, layout,
                                                                  override_projects_root, monkeypatch):
    from capture.project_manager import image_output_dir
    from app.core.storage_ops import relocate_images_and_commit
    from app.models.record import RecordImage

    files_before = _files(override_projects_root)
    real_move = shutil.move
    count = {"masters": 0}

    def move(src, dst, *args, **kw):
        if not str(src).endswith("_preview.jpg"):
            count["masters"] += 1
            if count["masters"] == 2:
                raise KeyboardInterrupt()
        return real_move(src, dst, *args, **kw)

    monkeypatch.setattr("app.core.storage_ops.shutil.move", move)
    target = image_output_dir("proj", layout["b"].id)
    moves = [(i, target) for i in db_session.query(RecordImage).order_by(RecordImage.id).all()]
    with pytest.raises(KeyboardInterrupt):
        relocate_images_and_commit(db_session, moves, [], "test move")
    assert _files(override_projects_root) == files_before


def _assert_left_at_new_paths_and_logged(layout, new_dir, caplog):
    """Every file sits at its new path and every (original, new) pair is logged at ERROR."""
    names = ["IMG_0001.CR2", "IMG_0001_preview.jpg", "IMG_0002.CR2", "IMG_0002_preview.jpg", "IMG_0003.jpg"]
    assert sorted(p.name for p in new_dir.iterdir()) == names
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    src = layout["src_dir"].resolve()
    for name in names:
        assert any(str(src / name) in m and str(new_dir.resolve() / name) in m for m in errors), name
    assert len(list(layout["thumb_dir"].iterdir())) == 3


@pytest.mark.unit
def test_a_failed_commit_whose_rollback_also_fails_is_an_unknown_outcome(client, db_session, layout,
                                                                         monkeypatch, caplog):
    """The rows the session flushed are still visible to it, so a re-read cannot tell what landed."""
    from sqlalchemy.exc import OperationalError
    from capture.project_manager import image_output_dir

    real_rollback = db_session.rollback

    def commit():
        db_session.flush()
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    def rollback():
        raise OperationalError("ROLLBACK", {}, Exception("connection gone"))

    monkeypatch.setattr(db_session, "commit", commit)
    monkeypatch.setattr(db_session, "rollback", rollback)
    with caplog.at_level(logging.ERROR, logger="app.core.storage_ops"):
        resp = ROUTES["move_records"][0](_as_operator(client), layout)
    monkeypatch.setattr(db_session, "rollback", real_rollback)
    real_rollback()

    assert resp.status_code == 500, resp.text
    assert "could not be determined" in resp.json()["detail"]
    _assert_left_at_new_paths_and_logged(layout, image_output_dir("proj", layout["b"].id), caplog)


@pytest.mark.unit
def test_an_interrupt_during_the_commit_moves_nothing_and_propagates(db_session, layout, monkeypatch, caplog):
    from capture.project_manager import image_output_dir
    from app.core.storage_ops import relocate_images_and_commit
    from app.models.record import RecordImage

    def commit():
        raise KeyboardInterrupt()

    target = image_output_dir("proj", layout["b"].id)
    moves = [(i, target) for i in db_session.query(RecordImage).order_by(RecordImage.id).all()]
    monkeypatch.setattr(db_session, "commit", commit)
    with caplog.at_level(logging.ERROR, logger="app.core.storage_ops"):
        with pytest.raises(KeyboardInterrupt):
            relocate_images_and_commit(db_session, moves, [], "test move")

    _assert_left_at_new_paths_and_logged(layout, target, caplog)


@pytest.mark.unit
def test_no_moved_image_never_reads_as_landed(db_session):
    from app.core.storage_ops import _relocation_landed

    assert _relocation_landed(db_session, {}) is None
