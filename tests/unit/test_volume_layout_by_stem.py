"""A volume with a file name stem keeps its files in `<projects root>/<stem>/`.

The stem layout has four directories and no project level:

    <stem>/captures/     masters
    <stem>/previews/     the `_preview.jpg` of each RAW master
    <stem>/thumbnails/   thumbnails
    <stem>/metadata/     the volume's own manifest.jsonl

A volume without a stem keeps the name-based layout,
`<project>/<name>/images/main/`, with each sidecar beside its master and the
project's `metadata/manifest.jsonl`. Renaming a project, moving a volume
between projects or re-parenting it moves none of a stemmed volume's files.

`app.core.volume_layout.migrate_volume_layout` moves the files of each
stemmed volume from the name-based layout into its stem layout, attributing
each file by its row, under a journal that a later run completes after a
crash. Volumes without a stem are left where they are.

Files are real, under the per-test projects root from `override_projects_root`.
The stems are test values in the `xx-test-` form.
"""

import hashlib
import json
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
    """A writable data directory for the journal and the sidecar list (the default /var/lib/dtk is not)."""
    from app.core import config

    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(config.settings, "DTK_DATA_DIR", str(d))
    return d


def _as_admin(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="adm", email="adm@example.com", hashed_password="x", role="admin", is_active=True)
    return client


def _tree(root: Path) -> dict:
    """Every file under `root`: its relative path, inode, mtime and bytes."""
    return {str(p.relative_to(root)): (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()}


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
def test_a_stemmed_volume_captures_into_its_stem_directory(override_projects_root, capture_settings):
    from capture.project_manager import capture_manifest_root, image_output_dir, volume_root

    assert volume_root("xx-test-001") == override_projects_root / "xx-test-001"
    assert image_output_dir("Mi Proyecto", "Tomo 1", "xx-test-001") == override_projects_root / "xx-test-001" / "captures"
    assert capture_manifest_root("Mi Proyecto", "xx-test-001") == override_projects_root / "xx-test-001"


@pytest.mark.unit
def test_a_volume_without_a_stem_and_a_project_level_capture_keep_the_name_based_layout(
        override_projects_root, capture_settings):
    from capture.project_manager import capture_manifest_root, image_output_dir

    assert image_output_dir("Mi Proyecto", "Tomo 1") == override_projects_root / "mi_proyecto" / "tomo_1" / "images" / "main"
    assert image_output_dir("Mi Proyecto") == override_projects_root / "mi_proyecto" / "images" / "main"
    assert capture_manifest_root("Mi Proyecto") == override_projects_root / "mi_proyecto"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["../etc", "Xx-Test", "xx/test", "", "a" * 28, 7])
def test_a_value_that_is_not_a_stem_names_no_directory(capture_settings, bad):
    from capture.project_manager import volume_root

    with pytest.raises(ValueError):
        volume_root(bad)


@pytest.mark.unit
def test_a_sidecar_is_in_previews_in_the_stem_layout_and_beside_its_master_otherwise():
    from app.core.paths import preview_sidecar_of

    assert preview_sidecar_of(Path("/r/xx-test-001/captures/f.CR2")) == Path("/r/xx-test-001/previews/f_preview.jpg")
    assert preview_sidecar_of(Path("/r/p/tomo/images/main/f.CR2")) == Path("/r/p/tomo/images/main/f_preview.jpg")
    # A volume without a stem named "captures" still has its masters in images/main.
    assert preview_sidecar_of(Path("/r/p/captures/images/main/f.CR2")) == Path("/r/p/captures/images/main/f_preview.jpg")


@pytest.mark.unit
def test_the_capture_target_names_the_stem_of_a_volume_that_has_one(db_session, capture_settings):
    from app.api.cameras import _resolve_capture_target
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    a = Collection(name="Tomo 1", project_id=p.id, filename_stem="xx-test-001")
    b = Collection(name="Tomo 1", project_id=p.id)
    db_session.add_all([a, b])
    db_session.commit()
    child = Collection(name="Hijo", parent_collection_id=a.id, filename_stem="xx-test-002")
    db_session.add(child)
    db_session.commit()

    assert _resolve_capture_target(db_session, "ignored", a.id) == ("P", "Tomo 1", "xx-test-001")
    assert _resolve_capture_target(db_session, "ignored", b.id) == ("P", "Tomo 1", None)
    assert _resolve_capture_target(db_session, "ignored", child.id) == ("P", "Hijo", "xx-test-002")
    assert _resolve_capture_target(db_session, "P", None) == ("P", None, None)


# ---------------------------------------------------------------------------
# Capture into the stem layout
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Writes a master where capture asks, and for a RAW master its sidecar where preview_sidecar_of names it."""

    def __init__(self, raw: bool):
        self.raw = raw

    def capture_image(self, output_path, camera_config, capture_output=False):
        from app.core.paths import preview_sidecar_of

        out = Path(output_path)
        if self.raw:
            out = out.with_suffix(".cr2")
            out.write_bytes(b"raw bytes of " + out.name.encode())
            preview_sidecar_of(out).write_bytes(b"preview of " + out.name.encode())
        else:
            out.write_bytes(b"jpeg bytes of " + out.name.encode())
        return out, None


@pytest.mark.unit
@pytest.mark.parametrize("raw", [False, True])
def test_a_capture_into_a_stemmed_volume_writes_its_files_and_manifest_in_the_stem_layout(
        override_projects_root, capture_settings, mock_camera_config, monkeypatch, raw):
    import capture.service as service
    from app.core.integrity import build_fixity_index

    monkeypatch.setattr(service, "get_backend", lambda: _FakeBackend(raw))

    path, capture_id, _ = service.single_capture_image(
        "P", mock_camera_config, check_camera=False, collection_name="Tomo 1", stem="xx-test-001",
        filename_index="xx-test-001-img_0001")

    vol = override_projects_root / "xx-test-001"
    master = Path(str(path))
    assert master.parent == vol / "captures"
    assert sorted(p.name for p in vol.iterdir()) == ["captures", "metadata", "previews", "thumbnails"]
    if raw:
        assert (vol / "previews" / f"{master.stem}_preview.jpg").read_bytes() == b"preview of " + master.name.encode()
    else:
        assert list((vol / "previews").iterdir()) == []
    lines = [json.loads(l) for l in (vol / "metadata" / "manifest.jsonl").read_text().splitlines()]
    assert [l["capture_id"] for l in lines] == [capture_id]
    # A RAW master is followed by its preview (tests/unit/test_preview_digests.py).
    assert [f["relative_path"] for f in lines[0]["files"]] == [f"captures/{master.name}"] + (
        [f"previews/{master.stem}_preview.jpg"] if raw else [])
    assert not (override_projects_root / "p").exists()
    index = build_fixity_index(override_projects_root.resolve())
    assert index.expected(capture_id, None, master.resolve()) == hashlib.sha256(master.read_bytes()).hexdigest()


@pytest.mark.unit
def test_a_capture_into_a_volume_without_a_stem_keeps_the_name_based_layout(
        override_projects_root, capture_settings, mock_camera_config, monkeypatch):
    import capture.service as service

    monkeypatch.setattr(service, "get_backend", lambda: _FakeBackend(raw=True))

    path, capture_id, _ = service.single_capture_image(
        "P", mock_camera_config, check_camera=False, collection_name="Tomo 1",
        filename_index="tomo-1-img_0001")

    master = Path(str(path))
    assert master.parent == override_projects_root / "p" / "tomo_1" / "images" / "main"
    assert (master.parent / f"{master.stem}_preview.jpg").exists()
    lines = (override_projects_root / "p" / "metadata" / "manifest.jsonl").read_text().splitlines()
    assert [json.loads(l)["capture_id"] for l in lines] == [capture_id]


@pytest.mark.unit
def test_a_dual_capture_into_a_stemmed_volume_writes_one_record_in_the_volume_manifest(
        override_projects_root, capture_settings, mock_camera_config, monkeypatch):
    import dataclasses
    import capture.service as service

    monkeypatch.setattr(service, "get_backend", lambda: _FakeBackend(raw=True))
    cam1 = dataclasses.replace(mock_camera_config, camera_index=1)

    p0, p1, capture_id, _ = service.dual_capture_image(
        "P", mock_camera_config, cam1, check_camera=False, stagger_ms=0, stem="xx-test-001",
        filename_index="xx-test-001-img_0001")

    vol = override_projects_root / "xx-test-001"
    assert {Path(str(p0)).parent, Path(str(p1)).parent} == {vol / "captures"}
    assert len(list((vol / "previews").iterdir())) == 2
    lines = [json.loads(l) for l in (vol / "metadata" / "manifest.jsonl").read_text().splitlines()]
    assert [l["capture_id"] for l in lines] == [capture_id]
    assert sorted(f["relative_path"] for f in lines[0]["files"]) == sorted(
        [f"captures/{Path(str(p)).name}" for p in (p0, p1)]
        + [f"previews/{Path(str(p)).stem}_preview.jpg" for p in (p0, p1)])


@pytest.mark.unit
def test_the_capture_route_writes_the_thumbnail_into_the_volume_thumbnails(
        client, db_session, capture_settings, override_projects_root, monkeypatch):
    """A RAW capture through POST /cameras/capture: master, preview, thumbnail and manifest in `<stem>/`."""
    import io
    import capture.service as service
    import capture.project_manager as project_manager_module
    from PIL import Image
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import RecordImage
    from app.core.paths import preview_sidecar_of

    jpeg = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(jpeg, format="JPEG")

    class Backend:
        def capture_image(self, output_path, camera_config, capture_output=False):
            out = Path(output_path).with_suffix(".cr2")
            out.write_bytes(b"raw")
            preview_sidecar_of(out).write_bytes(jpeg.getvalue())
            return out, None

    monkeypatch.setattr(service, "get_backend", lambda: Backend())
    monkeypatch.setattr(service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(project_manager_module, "default_camera_config_from_registry",
                        lambda camera_index, resolution="high", registry=None: (
                            {"camera_index": camera_index, "rotate_deg": 0}, "hw"))
    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    vol = Collection(name="Tomo 1", project_id=p.id, filename_stem="xx-test-001")
    db_session.add(vol)
    db_session.commit()

    resp = _as_admin(client).post("/cameras/capture",
                                  json={"project_name": "P", "camera_index": 0, "collection_id": vol.id})

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    img = db_session.query(RecordImage).one()
    root = override_projects_root / "xx-test-001"
    assert Path(img.file_path).parent == root / "captures"
    assert Path(img.thumbnail_path).parent == root / "thumbnails"
    assert preview_sidecar_of(Path(img.file_path)).parent == root / "previews"
    assert (root / "metadata" / "manifest.jsonl").exists()


# ---------------------------------------------------------------------------
# A stemmed volume's files stay where they are on renames and moves
# ---------------------------------------------------------------------------

@pytest.fixture
def placed(db_session, capture_settings, override_projects_root):
    """Projects P and Q; volumes A (stem xx-test-a) and B in P, C in Q, H in Q.

    A holds a record with a RAW master in `xx-test-a/captures/`, its preview in
    `previews/`, its thumbnail in `thumbnails/`, and the volume manifest. P's
    project directory exists, as project initialisation leaves it.
    """
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    p, q = Project(name="P"), Project(name="Q")
    db_session.add_all([p, q])
    db_session.commit()
    a = Collection(name="A", project_id=p.id, filename_stem="xx-test-a")
    b = Collection(name="B", project_id=p.id)
    c = Collection(name="C", project_id=q.id)
    db_session.add_all([a, b, c])
    db_session.commit()
    vol = override_projects_root / "xx-test-a"
    for d in ("captures", "previews", "thumbnails", "metadata"):
        (vol / d).mkdir(parents=True)
    master = vol / "captures" / "IMG_0001.CR2"
    master.write_bytes(b"raw")
    (vol / "previews" / "IMG_0001_preview.jpg").write_bytes(b"preview")
    thumb = vol / "thumbnails" / "IMG_0001_thumb.jpg"
    thumb.write_bytes(b"thumb")
    (vol / "metadata" / "manifest.jsonl").write_text("{}\n")
    (override_projects_root / "p" / "packages").mkdir(parents=True)
    (override_projects_root / "p" / "metadata").mkdir(parents=True)
    (override_projects_root / "p" / "metadata" / "project_manifest.jsonl").write_text("{}\n")
    rec = Record(title="r", collection_id=a.id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=master.name, file_path=str(master),
                               thumbnail_path=str(thumb), format="cr2"))
    db_session.commit()
    return {"p": p, "q": q, "a": a, "b": b, "c": c, "master": master, "vol": vol}


def _stem_files(placed) -> dict:
    return _files(placed["vol"])


@pytest.mark.unit
def test_a_volume_rename_moves_no_file(client, db_session, placed):
    files, rows = _stem_files(placed), _rows(db_session)

    resp = _as_admin(client).patch(f"/collections/{placed['a'].id}", json={"name": "A renamed"})

    assert resp.status_code == 200, resp.text
    assert _stem_files(placed) == files
    assert _rows(db_session) == rows


@pytest.mark.unit
@pytest.mark.parametrize("parent", ["b", "c"])
def test_a_reparent_within_or_across_projects_moves_no_file(client, db_session, placed, parent):
    files, rows = _stem_files(placed), _rows(db_session)

    resp = _as_admin(client).patch(f"/collections/{placed['a'].id}",
                                   json={"parent_collection_id": placed[parent].id})

    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_collection_id"] == placed[parent].id
    assert _stem_files(placed) == files
    assert _rows(db_session) == rows


@pytest.mark.unit
def test_moving_a_volume_to_another_project_moves_no_file(client, db_session, placed, override_projects_root):
    files, rows = _files(override_projects_root), _rows(db_session)

    resp = _as_admin(client).post(f"/projects/{placed['p'].id}/move-collections",
                                  json={"target_project_id": placed["q"].id})

    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] == 2
    assert _files(override_projects_root) == files
    assert _rows(db_session) == rows


@pytest.mark.unit
def test_a_project_rename_touches_no_file_of_a_stemmed_volume(client, db_session, placed, override_projects_root):
    files, rows = _stem_files(placed), _rows(db_session)

    resp = _as_admin(client).put(f"/projects/{placed['p'].id}", json={"name": "P renamed"})

    assert resp.status_code == 200, resp.text
    assert _stem_files(placed) == files
    assert _rows(db_session) == rows
    # The project's own tree still moves with its name.
    assert (override_projects_root / "p_renamed" / "metadata" / "project_manifest.jsonl").exists()
    assert not (override_projects_root / "p").exists()


@pytest.fixture
def running_export():
    """Start a registry job for a collection that runs until the test ends."""
    import threading
    import time
    from app.core.export_jobs import export_jobs

    release = threading.Event()
    jobs = []

    def start(collection_id):
        job = export_jobs.start(collection_id, lambda j: release.wait(30))
        jobs.append(job)
        deadline = time.time() + 5
        while job.state == "queued" and time.time() < deadline:
            time.sleep(0.01)
        return job

    yield start
    release.set()
    deadline = time.time() + 5
    while any(j.state in ("queued", "running") for j in jobs) and time.time() < deadline:
        time.sleep(0.01)


@pytest.mark.unit
@pytest.mark.parametrize("change", ["rename", "reparent", "move_collections", "project_rename"])
def test_a_change_that_moves_no_file_of_a_stemmed_volume_is_not_refused_by_its_export(
        client, db_session, placed, running_export, change):
    """Its files do not move, so a running export of it is no reason to refuse."""
    running_export(placed["a"].id)
    api = _as_admin(client)
    if change == "rename":
        resp = api.patch(f"/collections/{placed['a'].id}", json={"name": "A again"})
    elif change == "reparent":
        resp = api.patch(f"/collections/{placed['a'].id}", json={"parent_collection_id": placed["c"].id})
    elif change == "move_collections":
        resp = api.post(f"/projects/{placed['p'].id}/move-collections", json={"target_project_id": placed["q"].id})
    else:
        resp = api.put(f"/projects/{placed['p'].id}", json={"name": "P renamed"})
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_a_rename_of_a_volume_without_a_stem_is_still_refused_by_its_export(client, db_session, placed,
                                                                           running_export):
    """Its name-based directory moves with the name, so its running export refuses the rename."""
    running_export(placed["b"].id)
    resp = _as_admin(client).patch(f"/collections/{placed['b'].id}", json={"name": "B renamed"})
    assert resp.status_code == 409, resp.text


@pytest.mark.unit
def test_moving_records_into_a_stemmed_volume_moves_their_files_into_its_layout(
        client, db_session, capture_settings, override_projects_root):
    """Records still move their files between volumes: master to captures/, sidecar to previews/."""
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    src_vol = Collection(name="Old", project_id=p.id)
    dst_vol = Collection(name="New", project_id=p.id, filename_stem="xx-test-new")
    db_session.add_all([src_vol, dst_vol])
    db_session.commit()
    d = override_projects_root / "p" / "old" / "images" / "main"
    d.mkdir(parents=True)
    (d / "F.CR2").write_bytes(b"raw")
    (d / "F_preview.jpg").write_bytes(b"preview")
    rec = Record(title="r", collection_id=src_vol.id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename="F.CR2", file_path=str(d / "F.CR2"), format="cr2"))
    db_session.commit()

    resp = _as_admin(client).post(f"/collections/{src_vol.id}/move-records?target_collection_id={dst_vol.id}")

    assert resp.status_code == 200, resp.text
    assert "not_moved" not in resp.json()
    vol = override_projects_root / "xx-test-new"
    assert (vol / "captures" / "F.CR2").read_bytes() == b"raw"
    assert (vol / "previews" / "F_preview.jpg").read_bytes() == b"preview"
    db_session.expire_all()
    assert Path(db_session.query(RecordImage).one().file_path) == (vol / "captures" / "F.CR2").resolve()


# ---------------------------------------------------------------------------
# Reading and deleting files in the stem layout
# ---------------------------------------------------------------------------

def _jpeg_bytes() -> bytes:
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.unit
def test_preview_file_and_thumbnail_of_a_stemmed_raw_master(client, db_session, placed):
    from app.models.record import RecordImage

    (placed["vol"] / "previews" / "IMG_0001_preview.jpg").write_bytes(_jpeg_bytes())
    img = db_session.query(RecordImage).one()
    img.thumbnail_path = None
    db_session.commit()
    api = _as_admin(client)

    preview = api.get(f"/records/images/{img.id}/preview")
    assert preview.status_code == 200, preview.text
    assert preview.content == _jpeg_bytes()
    master = api.get(f"/records/images/{img.id}/file")
    assert master.status_code == 200 and master.content == b"raw"
    thumb = api.get(f"/records/images/{img.id}/thumbnail")
    assert thumb.status_code == 200, thumb.text
    db_session.expire_all()
    assert Path(db_session.get(RecordImage, img.id).thumbnail_path).parent == placed["vol"] / "thumbnails"


@pytest.mark.unit
def test_deleting_a_stemmed_image_removes_its_master_preview_and_thumbnail(client, db_session, placed):
    from app.models.record import RecordImage

    img = db_session.query(RecordImage).one()

    resp = _as_admin(client).delete(f"/records/images/{img.id}")

    assert resp.status_code in (200, 204), resp.text
    vol = placed["vol"]
    assert not (vol / "captures" / "IMG_0001.CR2").exists()
    assert not (vol / "previews" / "IMG_0001_preview.jpg").exists()
    assert not (vol / "thumbnails" / "IMG_0001_thumb.jpg").exists()
    assert (vol / "metadata" / "manifest.jsonl").exists()


@pytest.mark.unit
def test_deleting_an_empty_stemmed_volume_removes_its_empty_directories(client, db_session, capture_settings,
                                                                       override_projects_root):
    """A stem never captured under reserves nothing; its directories, if empty, go with it."""
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    a = Collection(name="Same", project_id=p.id, filename_stem="xx-test-a")
    b = Collection(name="Same", project_id=p.id)
    db_session.add_all([a, b])
    db_session.commit()
    for d in ("captures", "previews", "thumbnails", "metadata"):
        (override_projects_root / "xx-test-a" / d).mkdir(parents=True)
    (override_projects_root / "p" / "same" / "images" / "main").mkdir(parents=True)

    resp = _as_admin(client).delete(f"/collections/{a.id}")

    assert resp.status_code == 204, resp.text
    assert not (override_projects_root / "xx-test-a").exists()
    assert (override_projects_root / "p" / "same" / "images" / "main").exists()


@pytest.mark.unit
def test_deleting_an_empty_volume_keeps_a_file_no_row_references(client, db_session, capture_settings,
                                                                caplog, override_projects_root):
    from app.models.collection import Collection
    from app.models.project import Project

    p = Project(name="P")
    db_session.add(p)
    db_session.commit()
    c = Collection(name="C", project_id=p.id)
    db_session.add(c)
    db_session.commit()
    main = override_projects_root / "p" / "c" / "images" / "main"
    main.mkdir(parents=True)
    (override_projects_root / "p" / "c" / "images" / "thumbnails").mkdir()
    stray = main / "stray.jpg"
    stray.write_bytes(b"no row")

    resp = _as_admin(client).delete(f"/collections/{c.id}")

    assert resp.status_code == 204, resp.text
    assert stray.read_bytes() == b"no row"
    assert not (override_projects_root / "p" / "c" / "images" / "thumbnails").exists()
    assert "stray.jpg" in caplog.text


# ---------------------------------------------------------------------------
# Migration of the name-based layout
# ---------------------------------------------------------------------------

STEMS = {"a1": "xx-test-a1", "a2": "xx-test-a2", "tomo": "xx-test-tomo1"}


@pytest.fixture
def legacy(db_session, capture_settings, data_dir, override_projects_root):
    """The name-based layout that the layout migration moves files out of.

    Project "Proj" (directory `proj`). Two top-level volumes both named
    "Fondo A" share `proj/fondo_a`; a volume "Tomo 1" nested in the first has
    `proj/tomo_1`. These three have stems. The first Fondo A holds a RAW
    capture with its sidecar and thumbnail; the second a JPEG with its
    thumbnail and a RAW+JPEG pair of one stem whose sidecar belongs to the
    RAW. Tomo 1 holds a JPEG whose thumbnail row names a file that is gone.
    "Fondo B" has no stem and holds a RAW capture with its sidecar and
    thumbnail in `proj/fondo_b`. A file no row references sits in the shared
    directory, and a project-level capture in `proj/images/main`.
    """
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    root = override_projects_root
    proj = Project(name="Proj")
    db_session.add(proj)
    db_session.commit()
    a1 = Collection(name="Fondo A", project_id=proj.id, filename_stem=STEMS["a1"])
    a2 = Collection(name="Fondo A", project_id=proj.id, filename_stem=STEMS["a2"])
    u = Collection(name="Fondo B", project_id=proj.id)
    db_session.add_all([a1, a2, u])
    db_session.commit()
    tomo = Collection(name="Tomo 1", parent_collection_id=a1.id, filename_stem=STEMS["tomo"])
    db_session.add(tomo)
    db_session.commit()

    shared = root / "proj" / "fondo_a" / "images" / "main"
    shared_thumbs = root / "proj" / "fondo_a" / "images" / "thumbnails"
    tomo_dir = root / "proj" / "tomo_1" / "images" / "main"
    u_dir = root / "proj" / "fondo_b" / "images" / "main"
    u_thumbs = root / "proj" / "fondo_b" / "images" / "thumbnails"
    project_dir = root / "proj" / "images" / "main"
    for d in (shared, shared_thumbs, tomo_dir, u_dir, u_thumbs, project_dir, root / "proj" / "metadata"):
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

    r9 = record(collection_id=u.id)
    (u_dir / "IMG_0009_preview.jpg").write_bytes(b"preview of IMG_0009")
    t9 = u_thumbs / "IMG_0009_thumb.jpg"
    t9.write_bytes(b"thumb of IMG_0009")
    i9 = image(r9, u_dir / "IMG_0009.CR2", t9)

    stray = shared / "stray.jpg"
    stray.write_bytes(b"no row")

    return {
        "root": root, "proj": proj, "a1": a1, "a2": a2, "tomo": tomo, "u": u,
        "i1": i1, "i2": i2, "i3a": i3a, "i3b": i3b, "i4": i4, "i5": i5, "i9": i9,
        "stray": stray, "shared": shared, "tomo_dir": tomo_dir, "u_dir": u_dir, "project_dir": project_dir,
    }


UNSTEMMED_END_STATE = {
    "proj/fondo_b/images/main/IMG_0009.CR2": b"bytes of IMG_0009.CR2",
    "proj/fondo_b/images/main/IMG_0009_preview.jpg": b"preview of IMG_0009",
    "proj/fondo_b/images/thumbnails/IMG_0009_thumb.jpg": b"thumb of IMG_0009",
}


def _expected_end_state(L, manifest: bytes = b"{}\n") -> dict:
    """Where each file is after a complete migration, relative to the root, with its bytes."""
    a1, a2, tomo = STEMS["a1"], STEMS["a2"], STEMS["tomo"]
    return {
        f"{a1}/captures/IMG_0001.CR2": b"bytes of IMG_0001.CR2",
        f"{a1}/previews/IMG_0001_preview.jpg": b"preview of IMG_0001",
        f"{a1}/thumbnails/IMG_0001_thumb.jpg": b"thumb of IMG_0001",
        f"{a2}/captures/IMG_0002.jpg": b"bytes of IMG_0002.jpg",
        f"{a2}/thumbnails/IMG_0002_thumb.jpg": b"thumb of IMG_0002",
        f"{a2}/captures/IMG_0003.CR2": b"bytes of IMG_0003.CR2",
        f"{a2}/captures/IMG_0003.jpg": b"bytes of IMG_0003.jpg",
        f"{a2}/previews/IMG_0003_preview.jpg": b"preview of IMG_0003",
        f"{tomo}/captures/0001.jpg": b"bytes of 0001.jpg",
        "proj/images/main/p.jpg": b"bytes of p.jpg",
        "proj/fondo_a/images/main/stray.jpg": b"no row",
        "proj/metadata/manifest.jsonl": manifest,
        **UNSTEMMED_END_STATE,
    }


def _on_disk(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _assert_migrated(db_session, L, manifest: bytes = b"{}\n"):
    from app.models.record import RecordImage

    root = L["root"]
    # The volumes' own manifests (copies of their records) are checked in test_volume_fixity_carried.
    on_disk = {k: v for k, v in _on_disk(root).items()
               if not (k.split("/")[0] in STEMS.values() and k.endswith("/metadata/manifest.jsonl"))}
    assert on_disk == _expected_end_state(L, manifest)
    db_session.expire_all()
    by_id = {i.id: i for i in db_session.query(RecordImage).all()}

    def at(key, rel):
        assert Path(by_id[L[key].id].file_path) == (root / rel).resolve(), key
        assert by_id[L[key].id].filename == Path(rel).name

    a1, a2, tomo = STEMS["a1"], STEMS["a2"], STEMS["tomo"]
    at("i1", f"{a1}/captures/IMG_0001.CR2")
    at("i2", f"{a2}/captures/IMG_0002.jpg")
    at("i3a", f"{a2}/captures/IMG_0003.CR2")
    at("i3b", f"{a2}/captures/IMG_0003.jpg")
    at("i4", f"{tomo}/captures/0001.jpg")
    assert by_id[L["i5"].id].file_path == str(L["project_dir"] / "p.jpg")
    assert by_id[L["i9"].id].file_path == str(L["u_dir"] / "IMG_0009.CR2")
    assert Path(by_id[L["i1"].id].thumbnail_path) == (root / a1 / "thumbnails/IMG_0001_thumb.jpg").resolve()
    assert Path(by_id[L["i2"].id].thumbnail_path) == (root / a2 / "thumbnails/IMG_0002_thumb.jpg").resolve()
    # A thumbnail row naming a missing file is cleared, so it is regenerated beside the moved master.
    assert by_id[L["i4"].id].thumbnail_path is None
    # The emptied name-based directory of Tomo 1 is removed; the shared one keeps the file no row names.
    assert not (root / "proj" / "tomo_1").exists()
    assert L["stray"].exists()


@pytest.mark.unit
def test_migration_moves_each_file_into_its_own_rows_volume_stem_layout(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout, read_layout_journal

    report = migrate_volume_layout(db_session)

    _assert_migrated(db_session, legacy)
    assert read_layout_journal() is None
    assert report["dry_run"] is False
    assert sorted(m["image_id"] for m in report["moved"]) == sorted(
        legacy[k].id for k in ("i1", "i2", "i3a", "i3b", "i4"))
    assert report["left_unreferenced"] == [str(legacy["stray"].resolve())]


@pytest.mark.unit
def test_each_of_the_four_directories_holds_what_it_should(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    migrate_volume_layout(db_session)

    vol = legacy["root"] / STEMS["a2"]
    assert sorted(p.name for p in (vol / "captures").iterdir()) == ["IMG_0002.jpg", "IMG_0003.CR2", "IMG_0003.jpg"]
    assert sorted(p.name for p in (vol / "previews").iterdir()) == ["IMG_0003_preview.jpg"]
    assert sorted(p.name for p in (vol / "thumbnails").iterdir()) == ["IMG_0002_thumb.jpg"]
    # Nothing was renamed, so no relocation record was needed and no volume manifest written:
    # the capture records stay in the project's manifest, which is still read.
    assert not (vol / "metadata").exists()


@pytest.mark.unit
def test_a_volume_without_a_stem_is_left_exactly_where_it_is(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    before = db_session.get(RecordImage, legacy["i9"].id)
    row = (before.file_path, before.filename, before.thumbnail_path)

    report = migrate_volume_layout(db_session)

    on_disk = _on_disk(legacy["root"])
    for rel, data in UNSTEMMED_END_STATE.items():
        assert on_disk[rel] == data
    db_session.expire_all()
    after = db_session.get(RecordImage, legacy["i9"].id)
    assert (after.file_path, after.filename, after.thumbnail_path) == row
    assert legacy["i9"].id not in [m["image_id"] for m in report["moved"]]
    assert legacy["i9"].id not in [s["image_id"] for s in report["skipped"]]
    assert not any("IMG_0009" in p for p in report["left_unreferenced"])


@pytest.mark.unit
def test_a_stem_set_later_is_migrated_at_the_next_run(client, db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    migrate_volume_layout(db_session)
    resp = _as_admin(client).patch(f"/collections/{legacy['u'].id}", json={"filename_stem": "xx-test-b"})
    assert resp.status_code == 200, resp.text
    # Setting the stem moves nothing: the files wait for the next run.
    assert (legacy["u_dir"] / "IMG_0009.CR2").exists()

    report = migrate_volume_layout(db_session)

    assert [m["image_id"] for m in report["moved"]] == [legacy["i9"].id]
    vol = legacy["root"] / "xx-test-b"
    assert (vol / "captures" / "IMG_0009.CR2").read_bytes() == b"bytes of IMG_0009.CR2"
    assert (vol / "previews" / "IMG_0009_preview.jpg").read_bytes() == b"preview of IMG_0009"
    assert (vol / "thumbnails" / "IMG_0009_thumb.jpg").read_bytes() == b"thumb of IMG_0009"
    assert not (legacy["root"] / "proj" / "fondo_b").exists()
    db_session.expire_all()
    img = db_session.get(RecordImage, legacy["i9"].id)
    assert Path(img.file_path) == (vol / "captures" / "IMG_0009.CR2").resolve()
    assert Path(img.thumbnail_path) == (vol / "thumbnails" / "IMG_0009_thumb.jpg").resolve()


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
@pytest.mark.parametrize("crash_at", [1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_a_crash_at_any_move_is_completed_by_the_next_run(db_session, legacy, monkeypatch, crash_at):
    """The legacy layout takes nine moves: five masters, two sidecars, two thumbnails."""
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
def test_the_legacy_layout_takes_nine_moves(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl

    real_move = vl._move
    count = {"n": 0}

    def counting(src, dst):
        count["n"] += 1
        real_move(src, dst)

    monkeypatch.setattr(vl, "_move", counting)
    vl.migrate_volume_layout(db_session)
    assert count["n"] == 9


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

    target = legacy["root"] / STEMS["a2"] / "captures"
    target.mkdir(parents=True)
    occupant = target / "IMG_0002.jpg"
    occupant.write_bytes(b"already here")

    migrate_volume_layout(db_session)

    assert occupant.read_bytes() == b"already here"
    db_session.expire_all()
    moved = Path(db_session.get(RecordImage, legacy["i2"].id).file_path)
    assert moved.parent == target.resolve()
    assert moved.name == f"IMG_0002_img{legacy['i2'].id}.jpg"
    assert moved.read_bytes() == b"bytes of IMG_0002.jpg"
    assert db_session.get(RecordImage, legacy["i2"].id).filename == moved.name


@pytest.mark.unit
def test_a_raw_master_whose_sidecar_name_is_taken_in_previews_gets_its_image_suffix(db_session, legacy):
    from app.core.paths import preview_sidecar_of
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    previews = legacy["root"] / STEMS["a1"] / "previews"
    previews.mkdir(parents=True)
    (previews / "IMG_0001_preview.jpg").write_bytes(b"another preview")

    dry = migrate_volume_layout(db_session, dry_run=True)
    migrate_volume_layout(db_session)

    db_session.expire_all()
    master = Path(db_session.get(RecordImage, legacy["i1"].id).file_path)
    assert master.name == f"IMG_0001_img{legacy['i1'].id}.CR2"
    assert preview_sidecar_of(master).read_bytes() == b"preview of IMG_0001"
    assert (previews / "IMG_0001_preview.jpg").read_bytes() == b"another preview"
    assert str(master) in [m["to"] for m in dry["moved"]]


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

    thumbs = legacy["root"] / STEMS["a2"] / "thumbnails"
    thumbs.mkdir(parents=True)
    (thumbs / "IMG_0002_thumb.jpg").write_bytes(b"another thumbnail")
    old_thumb = legacy["root"] / "proj" / "fondo_a" / "images" / "thumbnails" / "IMG_0002_thumb.jpg"

    report = migrate_volume_layout(db_session)

    assert (thumbs / "IMG_0002_thumb.jpg").read_bytes() == b"another thumbnail"
    assert old_thumb.read_bytes() == b"thumb of IMG_0002"
    db_session.expire_all()
    assert db_session.get(RecordImage, legacy["i2"].id).thumbnail_path is None
    assert {"image_id": legacy["i2"].id, "reason": "name_taken"} in report["thumbnails_cleared"]
    assert str(old_thumb.resolve()) in report["left_unreferenced"]


@pytest.mark.unit
def test_the_command_line_dry_run_prints_the_report_and_changes_nothing(db_session, legacy, monkeypatch, capsys):
    import app.core.db as db_module
    from app.core.volume_layout import main

    files = _files(legacy["root"])
    monkeypatch.setattr(db_module, "SessionLocal", lambda: db_session)

    assert main(["--dry-run"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert len(report["moved"]) == 5
    assert _files(legacy["root"]) == files


# ---------------------------------------------------------------------------
# Rows decide; restores, retries, clashes, durability, fixity
# ---------------------------------------------------------------------------

def _capture_records(db_session, L) -> bytes:
    """Give each stemmed volume's image a capture id and its capture record in the project manifest; return the manifest."""
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
    for key, rel in (("i1", f"{STEMS['a1']}/captures/IMG_0001.CR2"),
                     ("i2", f"{STEMS['a2']}/captures/IMG_0002.jpg"),
                     ("i4", f"{STEMS['tomo']}/captures/0001.jpg")):
        assert str(root / rel) in report["left_unreferenced"], key


@pytest.mark.unit
def test_an_old_layout_row_found_after_a_migrated_startup_is_migrated(db_session, legacy, override_projects_root):
    import app.core.volume_layout as vl
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage

    vl.migrate_volume_layout_at_startup(db_session)
    c = Collection(name="Restored", project_id=legacy["proj"].id, filename_stem="xx-test-restored")
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
    assert Path(db_session.get(RecordImage, img.id).file_path) == (
        override_projects_root / "xx-test-restored" / "captures" / "R_0001.jpg").resolve()


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

    target = legacy["root"] / STEMS["a2"] / "captures"
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

    a1 = (legacy["root"] / STEMS["a1"]).resolve()
    assert a1 / "captures" in at_commit[0]
    assert a1 / "previews" in at_commit[0]
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


@pytest.fixture
def renamed_capture(db_session, legacy):
    """IMG_0002 with a capture record in the project manifest, and its name taken in the target."""
    from app.models.record import RecordImage

    img = db_session.get(RecordImage, legacy["i2"].id)
    img.capture_id = "cap-0002"
    db_session.commit()
    sha = hashlib.sha256(Path(img.file_path).read_bytes()).hexdigest()
    (legacy["root"] / "proj" / "metadata" / "manifest.jsonl").write_text(json.dumps(
        {"capture_id": "cap-0002", "files": [{"relative_path": "fondo_a/images/main/IMG_0002.jpg", "sha256": sha}]}
    ) + "\n")
    target = legacy["root"] / STEMS["a2"] / "captures"
    target.mkdir(parents=True)
    (target / "IMG_0002.jpg").write_bytes(b"unrelated")
    return img


@pytest.mark.unit
def test_a_renamed_master_keeps_its_capture_time_fixity(db_session, legacy, renamed_capture):
    """The relocation record goes in the volume's own manifest, its paths relative to `<stem>/`."""
    from app.core.integrity import verify_images_against_manifest
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    migrate_volume_layout(db_session)
    db_session.expire_all()
    img = db_session.get(RecordImage, renamed_capture.id)
    assert Path(img.file_path).name == f"IMG_0002_img{img.id}.jpg"
    vol_manifest = legacy["root"] / STEMS["a2"] / "metadata" / "manifest.jsonl"
    lines = [json.loads(l) for l in vol_manifest.read_text().splitlines()]
    # First the copy of its capture record, then the relocation record of the clash rename.
    assert [(l.get("record_type"), "copied_from" in l) for l in lines] == [(None, True), ("relocation", False)]
    assert lines[1]["from"] == "../proj/fondo_a/images/main/IMG_0002.jpg"
    assert lines[1]["to"] == f"captures/IMG_0002_img{img.id}.jpg"

    assert verify_images_against_manifest([img]) == []
    Path(img.file_path).write_bytes(b"bit rot")
    assert [m["record_image_id"] for m in verify_images_against_manifest([img])] == [img.id]


@pytest.mark.unit
def test_relocation_records_stay_out_of_the_bag_manifest(db_session, legacy, renamed_capture):
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
    assert [(l.get("record_type"), l["capture_id"]) for l in lines] == [(None, "cap-0002")]


# ---------------------------------------------------------------------------
# Export after the migration
# ---------------------------------------------------------------------------

def _approve(db_session, collection_id):
    from app.models.record import Record

    for r in db_session.query(Record).filter(Record.collection_id == collection_id).all():
        r.status = "approved"
    db_session.commit()


@pytest.mark.unit
def test_an_export_bag_after_the_migration_holds_the_moved_files_and_both_manifests_records(
        db_session, legacy, monkeypatch, tmp_path, mock_camera_config):
    """Records captured before the migration (project manifest) and after it (volume manifest) are both in the bag."""
    import zipfile
    import capture.service as service
    from app.core import config
    from app.core.export_jobs import ExportJob
    from app.core.export_service import _do_export
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import Record, RecordImage

    _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    # A capture after the migration, into the volume's own layout and manifest.
    monkeypatch.setattr(service, "get_backend", lambda: _FakeBackend(raw=False))
    path, capture_id, _ = service.single_capture_image(
        "Proj", mock_camera_config, check_camera=False, collection_name="Fondo A", stem=STEMS["a2"],
        filename_index=f"{STEMS['a2']}-img_0001")
    rec = Record(title="new", collection_id=legacy["a2"].id, status="approved", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=Path(str(path)).name, file_path=str(path),
                               format="jpg", role="single", capture_id=capture_id))
    db_session.commit()
    _approve(db_session, legacy["a2"].id)
    exports = config.settings.exports_dir

    job = ExportJob(legacy["a2"].id)
    _do_export(db_session, job, legacy["a2"].id)

    with zipfile.ZipFile(exports / job.zip_filename) as z:
        names = z.namelist()
        manifest = [json.loads(l) for l in z.read(next(n for n in names if n.endswith("data/metadata/manifest.jsonl")))
                    .decode().splitlines()]
        payload = {n: z.read(n) for n in names if "/data/" in n and n.endswith((".jpg", ".CR2"))}
    assert sorted(l["capture_id"] for l in manifest) == sorted(["cap-i2", "cap-i3a", "cap-i3b", capture_id])
    assert sorted(payload.values()) == sorted([b"bytes of IMG_0002.jpg", b"bytes of IMG_0003.CR2",
                                               b"bytes of IMG_0003.jpg", Path(str(path)).read_bytes()])


@pytest.mark.unit
def test_the_export_fixity_gate_checks_a_moved_file_after_the_migration(db_session, legacy):
    from app.core.export_jobs import ExportError
    from app.core.export_service import _fixity_gate
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    db_session.expire_all()
    img = db_session.get(RecordImage, legacy["i2"].id)

    _fixity_gate([img])
    Path(img.file_path).write_bytes(b"bit rot")
    with pytest.raises(ExportError) as err:
        _fixity_gate([img])
    assert err.value.detail["reason"] == "checksum_mismatch"


# ---------------------------------------------------------------------------
# A unit with both layouts
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_mixed_layout_unit_passes_the_integrity_check(db_session, legacy, monkeypatch, mock_camera_config):
    """Migrated volumes, a volume without a stem, and a new capture into a stemmed volume, side by side."""
    import capture.service as service
    from app.core.integrity import run_integrity_check
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import Record, RecordImage

    legacy["stray"].unlink()
    _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    monkeypatch.setattr(service, "get_backend", lambda: _FakeBackend(raw=True))
    path, capture_id, _ = service.single_capture_image(
        "Proj", mock_camera_config, check_camera=False, collection_name="Fondo A", stem=STEMS["a1"],
        filename_index=f"{STEMS['a1']}-img_0001")
    rec = Record(title="new", collection_id=legacy["a1"].id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    db_session.add(RecordImage(record_id=rec.id, filename=Path(str(path)).name, file_path=str(path),
                               format="cr2", role="single", capture_id=capture_id))
    db_session.commit()

    result = run_integrity_check(db_session, verify_hashes=True)

    assert result["missing_files"] == []
    assert result["orphan_files"] == []
    assert result["manifest_mismatches"] == []
    assert result["summary"]["hashes_checked"] == 6
    assert result["summary"]["ok"] is True


@pytest.mark.unit
def test_the_integrity_check_reports_an_orphaned_preview_in_the_stem_layout(db_session, legacy):
    """A preview whose RAW master is gone is reported, in `previews/` as it was beside the master."""
    from app.core.integrity import run_integrity_check
    from app.core.volume_layout import migrate_volume_layout

    legacy["stray"].unlink()
    migrate_volume_layout(db_session)
    orphan = legacy["root"] / STEMS["a1"] / "previews" / "GONE_preview.jpg"
    orphan.write_bytes(b"preview of a deleted master")

    result = run_integrity_check(db_session, verify_hashes=False)

    assert result["orphan_files"] == [str(orphan.resolve())]


@pytest.mark.unit
def test_a_renumber_renames_no_file_in_either_layout_on_one_unit(client, db_session, legacy):
    """A stemmed volume after the migration and one without a stem: sequences change, files and rows' paths do not."""
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import Record, RecordImage

    migrate_volume_layout(db_session)
    db_session.expire_all()
    before_rows = {i.id: (i.filename, i.file_path) for i in db_session.query(RecordImage).all()}
    before_files = _tree(legacy["root"])

    for key in ("a1", "u"):
        resp = _as_admin(client).post(f"/collections/{legacy[key].id}/images/renumber")
        assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert _tree(legacy["root"]) == before_files
    assert {i.id: (i.filename, i.file_path) for i in db_session.query(RecordImage).all()} == before_rows
    for key in ("a1", "u"):
        seqs = [r.sequence for r in db_session.query(Record).filter(Record.collection_id == legacy[key].id)]
        assert sorted(seqs) == list(range(1, len(seqs) + 1))
