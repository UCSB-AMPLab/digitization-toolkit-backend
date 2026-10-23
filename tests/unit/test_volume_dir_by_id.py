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


def _expected_end_state(L, manifest: bytes = b"{}\n") -> dict:
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
        "proj/metadata/manifest.jsonl": manifest,
    }


def _on_disk(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _assert_migrated(db_session, L, manifest: bytes = b"{}\n"):
    from app.models.record import RecordImage

    root = L["root"]
    assert _on_disk(root) == _expected_end_state(L, manifest)
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
    assert legacy["i2"].id not in [m["image_id"] for m in report["moved"]]
    assert legacy["i2"].id not in [s["image_id"] for s in report["skipped"]]


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


# ---------------------------------------------------------------------------
# Rows decide; restores, retries, clashes, durability, fixity
# ---------------------------------------------------------------------------

def _capture_records(db_session, L) -> bytes:
    """Give each collection image a capture id and its capture record in the project manifest; return the manifest."""
    import json
    from app.models.record import RecordImage

    lines = []
    for key in ("i1", "i2", "i3a", "i3b", "i4"):
        img = db_session.get(RecordImage, L[key].id)
        img.capture_id = f"cap-{key}"
        path = Path(img.file_path)
        rel = str(path.relative_to(L["root"] / "proj"))
        lines.append(json.dumps({"capture_id": img.capture_id, "files": [
            {"relative_path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]}))
    db_session.commit()
    manifest = ("\n".join(lines) + "\n").encode()
    (L["root"] / "proj" / "metadata" / "manifest.jsonl").write_bytes(manifest)
    return manifest


@pytest.mark.unit
def test_a_database_restored_from_before_the_migration_is_pointed_at_the_moved_files(db_session, legacy):
    """Each row is pointed at the moved file whose bytes match its capture record's checksum."""
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    manifest = _capture_records(db_session, legacy)
    before = {i.id: (i.file_path, i.filename, i.thumbnail_path) for i in db_session.query(RecordImage).all()}
    vl.migrate_volume_layout_at_startup(db_session)
    files = _files(legacy["root"])
    for img in db_session.query(RecordImage).all():
        img.file_path, img.filename, img.thumbnail_path = before[img.id]
    db_session.commit()

    report = vl.migrate_volume_layout(db_session)

    assert _files(legacy["root"]) == files  # no file moved
    assert sorted(a["image_id"] for a in report["adopted"]) == sorted(
        legacy[k].id for k in ("i1", "i2", "i3a", "i3b", "i4"))
    _assert_migrated(db_session, legacy, manifest)


@pytest.mark.unit
def test_a_restored_database_with_no_capture_checksums_is_not_pointed_at_files_by_name(db_session, legacy):
    """With no checksum to compare, a file of the row's name may be another page: the rows are reported missing."""
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    before = {i.id: (i.file_path, i.filename, i.thumbnail_path) for i in db_session.query(RecordImage).all()}
    vl.migrate_volume_layout_at_startup(db_session)
    files = _files(legacy["root"])
    for img in db_session.query(RecordImage).all():
        img.file_path, img.filename, img.thumbnail_path = before[img.id]
    db_session.commit()

    report = vl.migrate_volume_layout(db_session)

    assert _files(legacy["root"]) == files
    assert report["adopted"] == []
    keys = ("i1", "i2", "i3a", "i3b", "i4")
    assert sorted(s["image_id"] for s in report["skipped"] if s["reason"] == "missing") == sorted(legacy[k].id for k in keys)
    assert {i.id: i.file_path for i in db_session.query(RecordImage).all()} == {k: v[0] for k, v in before.items()}
    root = legacy["root"].resolve()
    for key, rel in (("i1", f"collection-{legacy['a1'].id}/images/main/IMG_0001.CR2"),
                     ("i2", f"collection-{legacy['a2'].id}/images/main/IMG_0002.jpg"),
                     ("i4", f"collection-{legacy['tomo'].id}/images/main/0001.jpg")):
        assert str(root / "proj" / rel) in report["left_unreferenced"], key


@pytest.mark.unit
def test_an_old_layout_row_found_after_a_migrated_startup_is_migrated(db_session, legacy, override_projects_root):
    import app.core.volume_layout as vl
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage
    from capture.project_manager import image_output_dir

    vl.migrate_volume_layout_at_startup(db_session)
    c = Collection(name="Restored", project_id=legacy["proj"].id)
    db_session.add(c)
    db_session.commit()
    old = override_projects_root / "proj" / "restored" / "images" / "main"
    old.mkdir(parents=True)
    f = old / "R_0001.jpg"
    f.write_bytes(b"restored")
    rec = Record(title="r", collection_id=c.id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    img = RecordImage(record_id=rec.id, filename=f.name, file_path=str(f), format="jpg", role="single")
    db_session.add(img)
    db_session.commit()

    vl.migrate_volume_layout_at_startup(db_session)

    db_session.expire_all()
    assert Path(db_session.get(RecordImage, img.id).file_path) == image_output_dir("Proj", c.id).resolve() / "R_0001.jpg"


@pytest.mark.unit
def test_a_startup_on_a_migrated_unit_touches_no_file_per_row(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    vl.migrate_volume_layout_at_startup(db_session)

    def no_disk(*a, **kw):
        raise AssertionError("the startup check looked at the disk")

    monkeypatch.setattr(vl, "resolve_within_storage", no_disk)
    monkeypatch.setattr(vl, "_plan", no_disk)
    report = vl.migrate_volume_layout(db_session)
    assert report["out_of_place"] == 0 and report["moved"] == []


@pytest.mark.unit
def test_a_sidecar_that_could_not_follow_is_reported_and_moved_by_a_later_run(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    real_move = vl._move

    def failing_sidecar(src, dst):
        if Path(dst).name.endswith("_preview.jpg"):
            raise OSError(5, "Input/output error")
        real_move(src, dst)

    monkeypatch.setattr(vl, "_move", failing_sidecar)
    first = vl.migrate_volume_layout(db_session)
    assert len(first["sidecars_not_moved"]) == 2
    second = vl.migrate_volume_layout(db_session)
    assert len(second["sidecars_pending"]) == 2  # still failing, still reported

    monkeypatch.setattr(vl, "_move", real_move)
    third = vl.migrate_volume_layout(db_session)

    assert len(third["sidecars_moved"]) == 2
    _assert_migrated(db_session, legacy)
    assert vl.migrate_volume_layout(db_session)["sidecars_pending"] == []


@pytest.mark.unit
def test_a_name_differing_only_in_case_is_taken_and_the_master_gets_its_image_suffix(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    target = legacy["root"] / "proj" / f"collection-{legacy['a2'].id}" / "images" / "main"
    target.mkdir(parents=True)
    (target / "img_0002.JPG").write_bytes(b"unrelated")

    dry = migrate_volume_layout(db_session, dry_run=True)
    migrate_volume_layout(db_session)

    db_session.expire_all()
    moved = Path(db_session.get(RecordImage, legacy["i2"].id).file_path)
    assert moved.name == f"IMG_0002_img{legacy['i2'].id}.jpg"
    assert moved.read_bytes() == b"bytes of IMG_0002.jpg"
    assert (target / "img_0002.JPG").read_bytes() == b"unrelated"
    assert str(moved) in [m["to"] for m in dry["moved"]]


@pytest.mark.unit
def test_an_unreadable_journal_refuses_to_serve(db_session, legacy, data_dir):
    import app.core.volume_layout as vl

    (data_dir / vl.JOURNAL_NAME).write_text('{"version": 2, "root": ', encoding="utf-8")

    with pytest.raises(vl.VolumeLayoutError):
        vl.migrate_volume_layout_at_startup(db_session)
    assert (data_dir / vl.JOURNAL_NAME).exists()


@pytest.mark.unit
def test_source_and_target_directories_are_fsynced_before_the_rows_are_committed(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    synced = []
    monkeypatch.setattr(vl, "_fsync_dir", lambda d: synced.append(Path(d)))
    at_commit = []
    real_commit = db_session.commit

    def commit():
        at_commit.append(list(synced))
        return real_commit()

    monkeypatch.setattr(db_session, "commit", commit)
    vl.migrate_volume_layout(db_session)

    a1 = (legacy["root"] / "proj" / f"collection-{legacy['a1'].id}" / "images").resolve()
    assert a1 / "main" in at_commit[0]
    assert a1 / "thumbnails" in at_commit[0]
    assert legacy["shared"].resolve() in at_commit[0]


@pytest.mark.unit
def test_a_move_across_filesystems_is_copied_flushed_and_only_then_unlinked(tmp_path, monkeypatch):
    import errno
    import os
    import app.core.volume_layout as vl

    src, dst = tmp_path / "a.jpg", tmp_path / "d" / "a.jpg"
    dst.parent.mkdir()
    src.write_bytes(b"bytes")
    events = []

    def exdev(*a):
        raise OSError(errno.EXDEV, "cross-device link")

    real_unlink = os.unlink
    monkeypatch.setattr(vl.os, "rename", exdev)
    monkeypatch.setattr("capture.utils._fsync_file", lambda p: events.append(("fsync_file", Path(p).name)))
    monkeypatch.setattr(vl, "_fsync_dir", lambda d: events.append(("fsync_dir", Path(d))))
    monkeypatch.setattr(vl.os, "unlink", lambda p: (events.append(("unlink", Path(p))), real_unlink(p)))

    vl._move(src, dst)

    assert dst.read_bytes() == b"bytes" and not src.exists()
    assert [e[0] for e in events] == ["fsync_file", "fsync_dir", "unlink"]


@pytest.mark.unit
def test_a_thumbnail_left_behind_with_its_row_cleared_is_reported_as_unreferenced(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    thumbs = legacy["root"] / "proj" / f"collection-{legacy['a2'].id}" / "images" / "thumbnails"
    thumbs.mkdir(parents=True)
    (thumbs / "IMG_0002_thumb.jpg").write_bytes(b"another thumbnail")
    old_thumb = legacy["root"] / "proj" / "fondo_a" / "images" / "thumbnails" / "IMG_0002_thumb.jpg"

    report = migrate_volume_layout(db_session)

    assert str(old_thumb.resolve()) in report["left_unreferenced"]


@pytest.mark.unit
def test_deleting_an_empty_collection_keeps_a_file_no_row_references(client, db_session, capture_settings, caplog):
    from app.models.collection import Collection
    from app.models.project import Project
    from capture.project_manager import collection_capture_root

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    c = Collection(name="C", project_id=p.id)
    db_session.add(c)
    db_session.commit()
    main = collection_capture_root("P", c.id) / "images" / "main"
    main.mkdir(parents=True)
    (collection_capture_root("P", c.id) / "images" / "thumbnails").mkdir()
    stray = main / "stray.jpg"
    stray.write_bytes(b"no row")

    resp = _as_operator(client).delete(f"/collections/{c.id}")

    assert resp.status_code == 204, resp.text
    assert stray.read_bytes() == b"no row"
    assert not (collection_capture_root("P", c.id) / "images" / "thumbnails").exists()
    assert "stray.jpg" in caplog.text


@pytest.fixture
def renamed_capture(db_session, legacy):
    """IMG_0002 with a capture record in the project manifest, and its name taken in the target."""
    import json
    from app.models.record import RecordImage

    img = db_session.get(RecordImage, legacy["i2"].id)
    img.capture_id = "cap-0002"
    db_session.commit()
    sha = hashlib.sha256(Path(img.file_path).read_bytes()).hexdigest()
    (legacy["root"] / "proj" / "metadata" / "manifest.jsonl").write_text(json.dumps(
        {"capture_id": "cap-0002", "files": [{"relative_path": "fondo_a/images/main/IMG_0002.jpg", "sha256": sha}]}
    ) + "\n")
    target = legacy["root"] / "proj" / f"collection-{legacy['a2'].id}" / "images" / "main"
    target.mkdir(parents=True)
    (target / "IMG_0002.jpg").write_bytes(b"unrelated")
    return img


@pytest.mark.unit
def test_a_renamed_master_keeps_its_capture_time_fixity(db_session, legacy, renamed_capture):
    import json
    from app.core.integrity import verify_images_against_manifest
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    migrate_volume_layout(db_session)
    db_session.expire_all()
    img = db_session.get(RecordImage, renamed_capture.id)
    assert Path(img.file_path).name == f"IMG_0002_img{img.id}.jpg"
    lines = [json.loads(l) for l in (legacy["root"] / "proj" / "metadata" / "manifest.jsonl").read_text().splitlines()]
    assert lines[-1]["record_type"] == "relocation"
    assert lines[-1]["from"] == "fondo_a/images/main/IMG_0002.jpg"
    assert lines[-1]["to"] == f"collection-{legacy['a2'].id}/images/main/IMG_0002_img{img.id}.jpg"

    assert verify_images_against_manifest([img]) == []
    Path(img.file_path).write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest([img])] == [img.id]


@pytest.mark.unit
def test_relocation_records_stay_out_of_the_bag_manifest(db_session, legacy, renamed_capture):
    import json
    from app.core.export_service import _filtered_manifest_blob
    from app.core.volume_layout import migrate_volume_layout
    from app.models.collection import Collection
    from app.models.record import Record

    migrate_volume_layout(db_session)
    db_session.expire_all()
    collection = db_session.get(Collection, legacy["a2"].id)
    records = db_session.query(Record).filter(Record.collection_id == collection.id).all()
    for r in records:
        for i in r.images:
            i.is_current = True

    blob = _filtered_manifest_blob(db_session, collection, records)

    lines = [json.loads(l) for l in blob.decode().splitlines()]
    assert [l.get("record_type") for l in lines] == [None]
