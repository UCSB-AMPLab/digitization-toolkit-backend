"""A collection's directory on disk is named by its id, not its name.

`image_output_dir(project_name, collection_id)` is
`<projects root>/<project dir>/collection-<id>/images/main`, flat under the
project whatever the nesting, so two collections of one name never share a
directory, a rename moves no file, and a re-parent inside one project moves
no file either. Only a change of project moves files, because the project
directory is still named after the project.

`app.core.volume_layout.migrate_volume_layout` moves files from the old
name-based directories into the id-based ones, attributing each file by its
row, under a journal that a later run completes after a crash.

Files are real, under the per-test projects root from `override_projects_root`.
"""

import hashlib
from pathlib import Path

import pytest


@pytest.fixture
def capture_settings(override_projects_root, monkeypatch):
    """Point capture.project_manager at this test's projects root."""
    import capture.project_manager as pm
    from app.core import config

    monkeypatch.setattr(pm, "settings", config.settings)


@pytest.fixture
def data_dir(override_projects_root, tmp_path, monkeypatch):
    """A writable data directory for the journal and marker (the default /var/lib/dtk is not)."""
    from app.core import config

    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(config.settings, "DTK_DATA_DIR", str(d))
    return d


def _as_operator(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x", role="operator", is_active=True)
    return client


def _files(root: Path) -> dict:
    """Every file under `root`, by path relative to it, with a digest of its bytes."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _rows(db) -> list:
    from app.models.record import RecordImage

    db.expire_all()
    return sorted((i.id, i.file_path, i.filename, i.thumbnail_path) for i in db.query(RecordImage).all())


# ---------------------------------------------------------------------------
# Path derivation
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_collection_directory_is_named_by_its_id(override_projects_root, capture_settings):
    from capture.project_manager import collection_capture_root, image_output_dir

    assert collection_capture_root("Mi Proyecto", 7) == override_projects_root / "mi_proyecto" / "collection-7"
    assert image_output_dir("Mi Proyecto", 7) == override_projects_root / "mi_proyecto" / "collection-7" / "images" / "main"


@pytest.mark.unit
def test_project_level_captures_keep_the_project_images_directory(override_projects_root, capture_settings):
    from capture.project_manager import image_output_dir

    assert image_output_dir("Mi Proyecto") == override_projects_root / "mi_proyecto" / "images" / "main"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["Fondo A", "7", True, 7.0])
def test_a_collection_name_is_refused_where_an_id_is_expected(capture_settings, bad):
    from capture.project_manager import collection_capture_root, image_output_dir

    with pytest.raises(TypeError):
        image_output_dir("P", bad)
    with pytest.raises(TypeError):
        collection_capture_root("P", bad)


@pytest.mark.unit
def test_two_collections_of_one_name_capture_into_distinct_directories(db_session, capture_settings):
    from app.api.cameras import _resolve_capture_target
    from app.models.collection import Collection
    from app.models.project import Project
    from capture.project_manager import image_output_dir

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    a = Collection(name="Tomo 1", project_id=p.id)
    b = Collection(name="Tomo 1", project_id=p.id)
    db_session.add_all([a, b])
    db_session.commit()

    target_a = _resolve_capture_target(db_session, "ignored", a.id)
    target_b = _resolve_capture_target(db_session, "ignored", b.id)

    assert target_a == ("P", a.id)
    assert target_b == ("P", b.id)
    assert image_output_dir(*target_a) != image_output_dir(*target_b)


@pytest.mark.unit
def test_a_nested_collection_directory_is_flat_under_the_project(db_session, capture_settings, override_projects_root):
    from app.api.cameras import _resolve_capture_target
    from app.models.collection import Collection
    from app.models.project import Project
    from capture.project_manager import image_output_dir

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    parent = Collection(name="Fondo", project_id=p.id)
    db_session.add(parent)
    db_session.commit()
    child = Collection(name="Tomo", parent_collection_id=parent.id)
    db_session.add(child)
    db_session.commit()

    assert image_output_dir(*_resolve_capture_target(db_session, "ignored", child.id)) == (
        override_projects_root / "p" / f"collection-{child.id}" / "images" / "main"
    )


# ---------------------------------------------------------------------------
# Rename and re-parent
# ---------------------------------------------------------------------------

@pytest.fixture
def placed(db_session, capture_settings):
    """Projects P and Q; collections A and B in P, C in Q; one record in A with
    a RAW master, its sidecar and a thumbnail, in A's id directory."""
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage
    from capture.project_manager import image_output_dir

    p, q = Project(name="P"), Project(name="Q")
    db_session.add_all([p, q])
    db_session.commit()
    a = Collection(name="A", project_id=p.id)
    b = Collection(name="B", project_id=p.id)
    c = Collection(name="C", project_id=q.id)
    db_session.add_all([a, b, c])
    db_session.commit()
    d = image_output_dir("P", a.id)
    d.mkdir(parents=True)
    master = d / "IMG_0001.CR2"
    master.write_bytes(b"raw")
    (d / "IMG_0001_preview.jpg").write_bytes(b"preview")
    thumbs = d.parent / "thumbnails"
    thumbs.mkdir()
    thumb = thumbs / "IMG_0001_thumb.jpg"
    thumb.write_bytes(b"thumb")
    rec = Record(title="r", collection_id=a.id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=master.name, file_path=str(master),
                               thumbnail_path=str(thumb), format="cr2"))
    db_session.commit()
    return {"p": p, "q": q, "a": a, "b": b, "c": c, "master": master}


@pytest.mark.unit
def test_a_rename_moves_no_file(client, db_session, placed, override_projects_root):
    files_before = _files(override_projects_root)
    rows_before = _rows(db_session)

    resp = _as_operator(client).patch(f"/collections/{placed['a'].id}", json={"name": "A renamed"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "A renamed"
    assert _files(override_projects_root) == files_before
    assert _rows(db_session) == rows_before


@pytest.mark.unit
def test_a_rename_to_the_name_of_a_sibling_moves_no_file(client, db_session, placed, override_projects_root):
    files_before = _files(override_projects_root)

    resp = _as_operator(client).patch(f"/collections/{placed['a'].id}", json={"name": "B"})

    assert resp.status_code == 200, resp.text
    assert _files(override_projects_root) == files_before


@pytest.mark.unit
def test_a_reparent_within_the_project_moves_no_file(client, db_session, placed, override_projects_root):
    files_before = _files(override_projects_root)
    rows_before = _rows(db_session)

    resp = _as_operator(client).patch(f"/collections/{placed['a'].id}",
                                      json={"parent_collection_id": placed["b"].id})

    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_collection_id"] == placed["b"].id
    assert _files(override_projects_root) == files_before
    assert _rows(db_session) == rows_before


@pytest.mark.unit
def test_a_reparent_into_another_project_moves_the_files_to_that_project(client, db_session, placed):
    from app.models.record import RecordImage
    from capture.project_manager import image_output_dir

    resp = _as_operator(client).patch(f"/collections/{placed['a'].id}",
                                      json={"parent_collection_id": placed["c"].id})

    assert resp.status_code == 200, resp.text
    new_dir = image_output_dir("Q", placed["a"].id)
    assert sorted(p.name for p in new_dir.iterdir()) == ["IMG_0001.CR2", "IMG_0001_preview.jpg"]
    assert not placed["master"].exists()
    db_session.expire_all()
    img = db_session.query(RecordImage).one()
    assert Path(img.file_path) == (new_dir / "IMG_0001.CR2").resolve()


# ---------------------------------------------------------------------------
# Migration of the name-based layout
# ---------------------------------------------------------------------------

@pytest.fixture
def legacy(db_session, capture_settings, data_dir, override_projects_root):
    """The name-based layout, written as the code before this change wrote it.

    Project "Proj" (directory `proj`). Two top-level collections both named
    "Fondo A" share `proj/fondo_a`; a collection "Tomo 1" nested in the first
    has `proj/tomo_1`. The first Fondo A holds a RAW capture with its sidecar
    and thumbnail; the second a JPEG with its thumbnail and a RAW+JPEG pair
    of one stem whose sidecar belongs to the RAW. Tomo 1 holds a JPEG whose
    thumbnail row names a file that is gone. A file no row references sits in
    the shared directory, and a project-level capture in `proj/images/main`.
    """
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    root = override_projects_root
    proj = Project(name="Proj")
    db_session.add(proj)
    db_session.commit()
    a1 = Collection(name="Fondo A", project_id=proj.id)
    a2 = Collection(name="Fondo A", project_id=proj.id)
    db_session.add_all([a1, a2])
    db_session.commit()
    tomo = Collection(name="Tomo 1", parent_collection_id=a1.id)
    db_session.add(tomo)
    db_session.commit()

    shared = root / "proj" / "fondo_a" / "images" / "main"
    shared_thumbs = root / "proj" / "fondo_a" / "images" / "thumbnails"
    tomo_dir = root / "proj" / "tomo_1" / "images" / "main"
    project_dir = root / "proj" / "images" / "main"
    for d in (shared, shared_thumbs, tomo_dir, project_dir, root / "proj" / "metadata"):
        d.mkdir(parents=True, exist_ok=True)
    (root / "proj" / "metadata" / "manifest.jsonl").write_text("{}\n")

    def record(collection_id=None, project_id=None):
        rec = Record(title="r", collection_id=collection_id, project_id=project_id,
                     status="captured", capture_mode="single")
        db_session.add(rec)
        db_session.commit()
        return rec

    def image(rec, path, thumb=None, role="single"):
        path.write_bytes(f"bytes of {path.name}".encode())
        img = RecordImage(record_id=rec.id, filename=path.name, file_path=str(path),
                          thumbnail_path=str(thumb) if thumb else None,
                          format=path.suffix.lstrip(".").lower(), role=role)
        db_session.add(img)
        db_session.commit()
        return img

    r1 = record(collection_id=a1.id)
    (shared / "IMG_0001_preview.jpg").write_bytes(b"preview of IMG_0001")
    t1 = shared_thumbs / "IMG_0001_thumb.jpg"
    t1.write_bytes(b"thumb of IMG_0001")
    i1 = image(r1, shared / "IMG_0001.CR2", t1)

    r2 = record(collection_id=a2.id)
    t2 = shared_thumbs / "IMG_0002_thumb.jpg"
    t2.write_bytes(b"thumb of IMG_0002")
    i2 = image(r2, shared / "IMG_0002.jpg", t2)

    r3 = record(collection_id=a2.id)
    (shared / "IMG_0003_preview.jpg").write_bytes(b"preview of IMG_0003")
    i3a = image(r3, shared / "IMG_0003.CR2", role="raw")
    i3b = image(r3, shared / "IMG_0003.jpg", role="single")

    r4 = record(collection_id=tomo.id)
    i4 = image(r4, tomo_dir / "0001.jpg", tomo_dir.parent / "thumbnails" / "0001_thumb.jpg")

    r5 = record(project_id=proj.id)
    i5 = image(r5, project_dir / "p.jpg")

    stray = shared / "stray.jpg"
    stray.write_bytes(b"no row")

    return {
        "root": root, "proj": proj, "a1": a1, "a2": a2, "tomo": tomo,
        "i1": i1, "i2": i2, "i3a": i3a, "i3b": i3b, "i4": i4, "i5": i5,
        "stray": stray, "shared": shared, "tomo_dir": tomo_dir, "project_dir": project_dir,
    }


def _expected_end_state(L) -> dict:
    """Where each file is after a complete migration, relative to the root, with its bytes."""
    a1, a2, tomo = (f"proj/collection-{L[k].id}" for k in ("a1", "a2", "tomo"))
    return {
        f"{a1}/images/main/IMG_0001.CR2": b"bytes of IMG_0001.CR2",
        f"{a1}/images/main/IMG_0001_preview.jpg": b"preview of IMG_0001",
        f"{a1}/images/thumbnails/IMG_0001_thumb.jpg": b"thumb of IMG_0001",
        f"{a2}/images/main/IMG_0002.jpg": b"bytes of IMG_0002.jpg",
        f"{a2}/images/thumbnails/IMG_0002_thumb.jpg": b"thumb of IMG_0002",
        f"{a2}/images/main/IMG_0003.CR2": b"bytes of IMG_0003.CR2",
        f"{a2}/images/main/IMG_0003.jpg": b"bytes of IMG_0003.jpg",
        f"{a2}/images/main/IMG_0003_preview.jpg": b"preview of IMG_0003",
        f"{tomo}/images/main/0001.jpg": b"bytes of 0001.jpg",
        "proj/images/main/p.jpg": b"bytes of p.jpg",
        "proj/fondo_a/images/main/stray.jpg": b"no row",
        "proj/metadata/manifest.jsonl": b"{}\n",
    }


def _on_disk(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _assert_migrated(db_session, L):
    from app.models.record import RecordImage

    root = L["root"]
    assert _on_disk(root) == _expected_end_state(L)
    db_session.expire_all()
    by_id = {i.id: i for i in db_session.query(RecordImage).all()}

    def at(key, rel):
        assert Path(by_id[L[key].id].file_path) == (root / rel).resolve(), key
        assert by_id[L[key].id].filename == Path(rel).name

    a1, a2, tomo = (f"proj/collection-{L[k].id}" for k in ("a1", "a2", "tomo"))
    at("i1", f"{a1}/images/main/IMG_0001.CR2")
    at("i2", f"{a2}/images/main/IMG_0002.jpg")
    at("i3a", f"{a2}/images/main/IMG_0003.CR2")
    at("i3b", f"{a2}/images/main/IMG_0003.jpg")
    at("i4", f"{tomo}/images/main/0001.jpg")
    assert by_id[L["i5"].id].file_path == str(L["project_dir"] / "p.jpg")
    assert Path(by_id[L["i1"].id].thumbnail_path) == (root / a1 / "images/thumbnails/IMG_0001_thumb.jpg").resolve()
    assert Path(by_id[L["i2"].id].thumbnail_path) == (root / a2 / "images/thumbnails/IMG_0002_thumb.jpg").resolve()
    # A thumbnail row naming a missing file is cleared, so it is regenerated beside the moved master.
    assert by_id[L["i4"].id].thumbnail_path is None
    # The emptied name-based directory of Tomo 1 is removed; the shared one keeps the file no row names.
    assert not (root / "proj" / "tomo_1").exists()
    assert L["stray"].exists()


@pytest.mark.unit
def test_migration_moves_each_file_to_its_own_rows_collection(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout, read_layout_journal

    report = migrate_volume_layout(db_session)

    _assert_migrated(db_session, legacy)
    assert read_layout_journal() is None
    assert report["dry_run"] is False
    assert sorted(m["image_id"] for m in report["moved"]) == sorted(
        legacy[k].id for k in ("i1", "i2", "i3a", "i3b", "i4"))
    assert report["left_unreferenced"] == [str(legacy["stray"].resolve())]


@pytest.mark.unit
def test_a_second_run_changes_nothing(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    migrate_volume_layout(db_session)
    files, rows = _files(legacy["root"]), _rows(db_session)

    report = migrate_volume_layout(db_session)

    assert report["moved"] == []
    assert _files(legacy["root"]) == files
    assert _rows(db_session) == rows


@pytest.mark.unit
def test_a_dry_run_reports_the_moves_and_changes_nothing(db_session, legacy, data_dir):
    from app.core.volume_layout import migrate_volume_layout

    files, rows = _files(legacy["root"]), _rows(db_session)
    dirs = sorted(str(p) for p in legacy["root"].rglob("*") if p.is_dir())

    dry = migrate_volume_layout(db_session, dry_run=True)

    assert dry["dry_run"] is True
    assert _files(legacy["root"]) == files
    assert sorted(str(p) for p in legacy["root"].rglob("*") if p.is_dir()) == dirs
    assert _rows(db_session) == rows
    assert list(data_dir.iterdir()) == []
    assert dry["left_unreferenced"] == [str(legacy["stray"].resolve())]

    real = migrate_volume_layout(db_session)
    key = lambda m: (m["image_id"], m["from"], m["to"])
    assert sorted(map(key, dry["moved"])) == sorted(map(key, real["moved"]))


class _Crash(BaseException):
    """Stands in for the process dying: not an Exception, so nothing catches it."""


@pytest.mark.unit
@pytest.mark.parametrize("crash_at", [1, 2, 4, 7])
def test_a_crash_mid_way_is_completed_by_the_next_run(db_session, legacy, monkeypatch, crash_at):
    import app.core.volume_layout as vl

    real_move = vl._move
    count = {"n": 0}

    def crashing_move(src, dst):
        count["n"] += 1
        if count["n"] == crash_at:
            raise _Crash()
        real_move(src, dst)

    monkeypatch.setattr(vl, "_move", crashing_move)
    with pytest.raises(_Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()  # the process died: nothing uncommitted survives
    assert vl.read_layout_journal() is not None

    monkeypatch.setattr(vl, "_move", real_move)
    report = vl.migrate_volume_layout(db_session)

    assert report["completed_journal"] is True
    _assert_migrated(db_session, legacy)
    assert vl.read_layout_journal() is None


@pytest.mark.unit
def test_a_crash_after_the_commit_is_completed_by_the_next_run(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    real_clear = vl.clear_layout_journal

    def crash():
        raise _Crash()

    monkeypatch.setattr(vl, "clear_layout_journal", crash)
    with pytest.raises(_Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(vl, "clear_layout_journal", real_clear)
    assert vl.read_layout_journal() is not None

    vl.migrate_volume_layout(db_session)

    _assert_migrated(db_session, legacy)


@pytest.mark.unit
def test_a_dry_run_with_a_pending_journal_reports_it_and_changes_nothing(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    real_move = vl._move
    count = {"n": 0}

    def crashing_move(src, dst):
        count["n"] += 1
        if count["n"] == 2:
            raise _Crash()
        real_move(src, dst)

    monkeypatch.setattr(vl, "_move", crashing_move)
    with pytest.raises(_Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(vl, "_move", real_move)
    files, rows, journal = _files(legacy["root"]), _rows(db_session), vl.read_layout_journal()

    dry = vl.migrate_volume_layout(db_session, dry_run=True)

    assert dry["pending_journal"] is True
    assert _files(legacy["root"]) == files
    assert _rows(db_session) == rows
    assert vl.read_layout_journal() == journal


@pytest.mark.unit
def test_migration_never_overwrites_a_file_in_the_target(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    target = legacy["root"] / "proj" / f"collection-{legacy['a2'].id}" / "images" / "main"
    target.mkdir(parents=True)
    occupant = target / "IMG_0002.jpg"
    occupant.write_bytes(b"already here")

    migrate_volume_layout(db_session)

    assert occupant.read_bytes() == b"already here"
    db_session.expire_all()
    moved = Path(db_session.get(RecordImage, legacy["i2"].id).file_path)
    assert moved.parent == target.resolve()
    assert moved.name != "IMG_0002.jpg"
    assert moved.name.startswith("IMG_0002_")
    assert moved.read_bytes() == b"bytes of IMG_0002.jpg"
    assert db_session.get(RecordImage, legacy["i2"].id).filename == moved.name


@pytest.mark.unit
def test_migration_leaves_files_outside_the_active_root_where_they_are(db_session, legacy, tmp_path):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    uploads = Path(legacy["root"].parent / "data" / "uploads")
    uploads.mkdir(parents=True)
    upload = uploads / "u.jpg"
    upload.write_bytes(b"upload")
    img = db_session.get(RecordImage, legacy["i2"].id)
    img.file_path = str(upload)
    img.thumbnail_path = None
    db_session.commit()

    report = migrate_volume_layout(db_session)

    assert upload.exists()
    db_session.expire_all()
    assert db_session.get(RecordImage, legacy["i2"].id).file_path == str(upload)
    assert {"image_id": legacy["i2"].id, "path": str(upload.resolve()), "reason": "outside_projects_root"} in report["skipped"]


@pytest.mark.unit
def test_migration_refuses_an_unusable_override_root_and_creates_nothing(db_session, legacy, monkeypatch, tmp_path):
    from app.core.storage_override import StorageUnavailableError
    from app.core.volume_layout import migrate_volume_layout

    files, rows = _files(legacy["root"]), _rows(db_session)
    missing = tmp_path / "mounts" / "usb" / "dtk-projects"
    monkeypatch.setattr("app.core.storage_override.get_storage_override", lambda: str(missing))

    with pytest.raises(StorageUnavailableError):
        migrate_volume_layout(db_session)

    assert not (tmp_path / "mounts").exists()
    assert _files(legacy["root"]) == files
    assert _rows(db_session) == rows


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_startup_migrates_once_and_then_skips_the_scan(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    vl.migrate_volume_layout_at_startup(db_session)
    _assert_migrated(db_session, legacy)

    def no_scan(*a, **kw):
        raise AssertionError("the layout was scanned again")

    monkeypatch.setattr(vl, "_plan", no_scan)
    vl.migrate_volume_layout_at_startup(db_session)


@pytest.mark.unit
def test_startup_with_an_unusable_root_logs_and_serves(db_session, legacy, monkeypatch, tmp_path, caplog):
    import app.core.volume_layout as vl

    missing = tmp_path / "mounts" / "usb" / "dtk-projects"
    monkeypatch.setattr("app.core.storage_override.get_storage_override", lambda: str(missing))

    vl.migrate_volume_layout_at_startup(db_session)

    assert not (tmp_path / "mounts").exists()
    assert "not available" in caplog.text


@pytest.mark.unit
def test_startup_refuses_to_serve_when_a_journal_cannot_be_completed(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    real_move = vl._move
    count = {"n": 0}

    def crashing_move(src, dst):
        count["n"] += 1
        if count["n"] == 2:
            raise _Crash()
        real_move(src, dst)

    monkeypatch.setattr(vl, "_move", crashing_move)
    with pytest.raises(_Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(vl, "_move", real_move)

    def failing_commit():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db_session, "commit", failing_commit)
    with pytest.raises(RuntimeError):
        vl.migrate_volume_layout_at_startup(db_session)
    assert vl.read_layout_journal() is not None


@pytest.mark.unit
def test_a_thumbnail_whose_name_is_taken_stays_and_its_row_is_cleared(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    thumbs = legacy["root"] / "proj" / f"collection-{legacy['a2'].id}" / "images" / "thumbnails"
    thumbs.mkdir(parents=True)
    (thumbs / "IMG_0002_thumb.jpg").write_bytes(b"another thumbnail")
    old_thumb = legacy["root"] / "proj" / "fondo_a" / "images" / "thumbnails" / "IMG_0002_thumb.jpg"

    report = migrate_volume_layout(db_session)

    assert (thumbs / "IMG_0002_thumb.jpg").read_bytes() == b"another thumbnail"
    assert old_thumb.read_bytes() == b"thumb of IMG_0002"
    db_session.expire_all()
    assert db_session.get(RecordImage, legacy["i2"].id).thumbnail_path is None
    assert {"image_id": legacy["i2"].id, "reason": "name_taken"} in report["thumbnails_cleared"]


@pytest.mark.unit
def test_the_command_line_dry_run_prints_the_report_and_changes_nothing(db_session, legacy, monkeypatch, capsys):
    import json
    import app.core.db as db_module
    from app.core.volume_layout import main

    files = _files(legacy["root"])
    monkeypatch.setattr(db_module, "SessionLocal", lambda: db_session)

    assert main(["--dry-run"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert len(report["moved"]) == 5
    assert _files(legacy["root"]) == files


@pytest.mark.unit
def test_deleting_an_empty_collection_removes_its_id_directory(client, db_session, capture_settings):
    from app.models.collection import Collection
    from app.models.project import Project
    from capture.project_manager import collection_capture_root

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    a = Collection(name="Same", project_id=p.id)
    b = Collection(name="Same", project_id=p.id)
    db_session.add_all([a, b])
    db_session.commit()
    for c in (a, b):
        (collection_capture_root("P", c.id) / "images" / "main").mkdir(parents=True)

    resp = _as_operator(client).delete(f"/collections/{a.id}")

    assert resp.status_code == 204, resp.text
    assert not collection_capture_root("P", a.id).exists()
    assert collection_capture_root("P", b.id).exists()
