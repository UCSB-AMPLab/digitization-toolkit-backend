"""Offsite image upload (NEH-227): keys, listing checks, verification, adoption, the received indicator.

The end-to-end paths run a real rclone against a local directory remote
(rclone's `local` backend lists SHA1). A wrapper script stands in front of the
real binary, records each call, and can be told to refuse a listing, fail a
copy, drop the checksums from a listing, or refuse listings once a copy has
succeeded. No bucket and no key is used; rclone is given an empty config file
so that it reads no configuration of this machine's.

Skipped when no rclone binary is found (DTK_TEST_RCLONE, else PATH), except
under CI (the CI environment variable set), where a missing rclone fails the
test: .github/workflows/ci.yml installs a pinned one for these tests.

    python -m pytest tests/unit/test_image_upload.py
"""

import hashlib
import json
import os
import shutil
import stat
import sys
import textwrap
from pathlib import Path

import pytest

REAL_RCLONE = os.environ.get("DTK_TEST_RCLONE") or shutil.which("rclone")

STEM = "xx-test-001"
TENANT = "captua-test"

_WRAPPER = '''#!{python}
import json, os, subprocess, sys
from pathlib import Path

ctl = Path(os.environ["STEP6_CTL"])
modes = set((ctl / "modes").read_text().split()) if (ctl / "modes").exists() else set()
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
with open(ctl / "calls", "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
copied = (ctl / "copied").exists()

if cmd == "lsjson" and ("listing-refused" in modes or ("refuse-after-copy" in modes and copied)):
    sys.stderr.write("ERROR : 401 unauthorized\\n")
    sys.exit(1)
if cmd == "copyto" and "copy-fails" in modes:
    sys.stderr.write("ERROR : connection reset\\n")
    sys.exit(1)

r = subprocess.run([os.environ["STEP6_REAL_RCLONE"]] + sys.argv[1:], capture_output=True, text=True)
out = r.stdout
if cmd == "lsjson" and r.returncode == 0 and "strip-hashes" in modes and (copied or "strip-always" in modes):
    rows = json.loads(out)
    for row in rows:
        row.pop("Hashes", None)
    out = json.dumps(rows)
if cmd == "copyto" and r.returncode == 0:
    (ctl / "copied").write_text("1")
sys.stdout.write(out)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
'''


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class Remote:
    def __init__(self, root: Path, ctl: Path):
        self.root = root
        self.ctl = ctl

    def set_modes(self, *modes):
        (self.ctl / "modes").write_text(" ".join(modes))
        (self.ctl / "copied").unlink(missing_ok=True)

    def calls(self, command=None):
        path = self.ctl / "calls"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in rows if command is None or r[0] == command]

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def put(self, key: str, data: bytes):
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@pytest.fixture
def remote(db_session, tmp_path, monkeypatch):
    """A local directory remote behind the wrapper, with OFFSITE_* pointing at it."""
    if not REAL_RCLONE or not os.access(REAL_RCLONE, os.X_OK):
        message = "no rclone binary (set DTK_TEST_RCLONE or put rclone on PATH)"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    from app.core import config

    root = tmp_path / "bucket"
    root.mkdir()
    ctl = tmp_path / "rclone-ctl"
    ctl.mkdir()
    wrapper = tmp_path / "rclone-wrapper"
    wrapper.write_text(_WRAPPER.format(python=sys.executable))
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    empty_config = tmp_path / "rclone.conf"
    empty_config.write_text("")
    data = tmp_path / "data"
    data.mkdir()

    monkeypatch.setenv("STEP6_CTL", str(ctl))
    monkeypatch.setenv("STEP6_REAL_RCLONE", REAL_RCLONE)
    s = config.settings
    monkeypatch.setattr(s, "OFFSITE_REMOTE", str(root))
    monkeypatch.setattr(s, "OFFSITE_TENANT", TENANT)
    monkeypatch.setattr(s, "OFFSITE_BWLIMIT", "0")
    monkeypatch.setattr(s, "RCLONE_BIN", str(wrapper))
    monkeypatch.setattr(s, "RCLONE_CONFIG", str(empty_config))
    monkeypatch.setattr(s, "DTK_DATA_DIR", str(data))
    return Remote(root, ctl)


def _volume(db, name="Tomo 1", stem=STEM):
    from app.models.collection import Collection
    from app.models.project import Project

    project = db.query(Project).filter(Project.name == "P").one_or_none()
    if project is None:
        project = Project(name="P")
        db.add(project)
        db.commit()
    vol = Collection(name=name, project_id=project.id, filename_stem=stem)
    db.add(vol)
    db.commit()
    return vol


def _image(db, vol, root: Path, name: str, data: bytes, *, preview: bytes = None,
           is_current=True, thumbnail=True):
    """A record with one image whose files are on disk in the volume's layout."""
    from app.models.record import Record, RecordImage

    if vol.filename_stem:
        base = root / vol.filename_stem
        master = base / "captures" / name
    else:
        base = root / "p" / "tomo" / "images"
        master = base / "main" / name
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(data)
    if preview is not None:
        from app.core.paths import preview_sidecar_of
        side = preview_sidecar_of(master)
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_bytes(preview)
    thumb = None
    if thumbnail:
        thumb = base / "thumbnails" / f"{Path(name).stem}_thumb.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"thumb " + name.encode())
    rec = Record(title=name, collection_id=vol.id, capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename=name, file_path=str(master),
                      thumbnail_path=str(thumb) if thumb else None,
                      format=Path(name).suffix.lstrip(".").upper(), is_current=is_current)
    db.add(img)
    db.commit()
    return img


def _rows(db):
    from app.models.offsite_object import OffsiteObject

    db.expire_all()
    return {(r.record_image_id, r.kind): r for r in db.query(OffsiteObject).all()}


def _run(db):
    from app.core.image_upload import run_image_upload

    return run_image_upload(db)


# ---------------------------------------------------------------------------
# Keys and configuration
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_keys_follow_the_contract_layout():
    from app.core.image_upload import capture_key, preview_key

    assert capture_key("sbmal", STEM, "f.CR2") == f"sbmal/{STEM}/captures/f.CR2"
    assert preview_key("sbmal", STEM, "f_preview.jpg") == f"sbmal/{STEM}/previews/f_preview.jpg"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["Sbmal", "a/b", "../x", "a--b", "a b"])
def test_a_tenant_that_is_not_a_prefix_is_refused(db_session, monkeypatch, bad):
    from app.core import config
    from app.core.image_upload import run_image_upload

    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "/nonexistent")
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", bad)
    assert run_image_upload(db_session)["status"] == "misconfigured"


@pytest.mark.unit
def test_an_empty_tenant_disables_the_upload(remote, db_session, override_projects_root, monkeypatch):
    from app.core import config
    from app.core.image_upload import image_upload_scheduler, is_enabled

    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "a.jpg", b"a")
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", "")

    assert _run(db_session) == {"status": "disabled"}
    assert remote.calls() == []
    assert remote.files() == {}
    assert is_enabled() is False
    assert image_upload_scheduler.start() is False
    assert not image_upload_scheduler.is_running()


@pytest.mark.unit
def test_an_empty_remote_disables_the_upload(remote, db_session, override_projects_root, monkeypatch):
    from app.core import config

    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "a.jpg", b"a")
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", "")
    assert _run(db_session) == {"status": "disabled"}
    assert remote.calls() == []


# ---------------------------------------------------------------------------
# A first run, and a second with nothing to do
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_first_run_uploads_captures_and_previews_and_verifies_each(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    raw = _image(db_session, vol, override_projects_root, "f1.CR2", b"raw one", preview=b"preview one")
    jpg = _image(db_session, vol, override_projects_root, "f2.jpg", b"jpeg two")

    result = _run(db_session)

    assert result["status"] == "done", result
    assert result["uploaded"] == 3
    assert remote.files() == {
        f"{TENANT}/{STEM}/captures/f1.CR2": b"raw one",
        f"{TENANT}/{STEM}/previews/f1_preview.jpg": b"preview one",
        f"{TENANT}/{STEM}/captures/f2.jpg": b"jpeg two",
    }
    rows = _rows(db_session)
    assert set(rows) == {(raw.id, "capture"), (raw.id, "preview"), (jpg.id, "capture")}
    for row in rows.values():
        assert row.verified_at is not None and row.uploaded_at is not None
        assert row.refused_at is None
    assert rows[(raw.id, "capture")].local_sha1 == _sha1(b"raw one")
    assert rows[(raw.id, "preview")].object_key == f"{TENANT}/{STEM}/previews/f1_preview.jpg"
    # Every upload is a copyto with --no-check-dest and the bandwidth cap.
    for call in remote.calls("copyto"):
        assert "--no-check-dest" in call and "--bwlimit" in call
    # No staging link is left behind.
    from app.core.image_upload import STAGING_DIR_NAME
    assert not (override_projects_root / STAGING_DIR_NAME).exists()


@pytest.mark.unit
def test_a_second_run_has_nothing_to_do(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.CR2", b"raw", preview=b"prev")
    _run(db_session)
    before = len(remote.calls())

    result = _run(db_session)

    assert result["status"] == "done"
    assert result["uploaded"] == result["already_present"] == 0
    assert remote.calls()[before:] == [], "no listing and no upload for verified objects"


@pytest.mark.unit
def test_thumbnails_are_never_uploaded(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"j", thumbnail=True)
    _run(db_session)
    assert not [k for k in remote.files() if "thumb" in k]


@pytest.mark.unit
def test_superseded_images_are_uploaded_too(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    old = _image(db_session, vol, override_projects_root, "f1.jpg", b"first take", is_current=False)
    new = _image(db_session, vol, override_projects_root, "f2.jpg", b"retake")
    _run(db_session)
    rows = _rows(db_session)
    assert rows[(old.id, "capture")].verified_at is not None
    assert rows[(new.id, "capture")].verified_at is not None


@pytest.mark.unit
def test_a_volume_without_a_stem_is_skipped(remote, db_session, override_projects_root):
    vol = _volume(db_session, name="Sin raíz", stem=None)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"j")
    result = _run(db_session)
    assert result["status"] == "done"
    assert remote.files() == {}
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_a_raw_master_without_a_preview_file_uploads_only_the_capture(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.CR2", b"raw")
    result = _run(db_session)
    assert result["uploaded"] == 1
    assert set(_rows(db_session)) == {(img.id, "capture")}


# ---------------------------------------------------------------------------
# Never overwrite
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_object_already_present_with_the_same_sha1_is_recorded_without_an_upload(
        remote, db_session, override_projects_root):
    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.jpg", b"same bytes")
    remote.put(f"{TENANT}/{STEM}/captures/f1.jpg", b"same bytes")

    result = _run(db_session)

    assert result["already_present"] == 1 and result["uploaded"] == 0
    assert remote.calls("copyto") == []
    row = _rows(db_session)[(img.id, "capture")]
    assert row.verified_at is not None and row.uploaded_at is None


@pytest.mark.unit
def test_a_different_sha1_at_the_key_stops_that_object_and_reports_both_files(
        remote, db_session, override_projects_root):
    from app.models.system_log import SystemLog

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.jpg", b"local bytes")
    other = _image(db_session, vol, override_projects_root, "f2.jpg", b"second")
    key = f"{TENANT}/{STEM}/captures/f1.jpg"
    remote.put(key, b"someone else's bytes")

    result = _run(db_session)

    assert result["status"] == "done"
    assert result["refused"] == [{
        "image_id": img.id, "kind": "capture", "reason": "remote_differs",
        "local_path": str(Path(img.file_path).resolve()), "local_sha1": _sha1(b"local bytes"),
        "key": key, "remote_sha1": _sha1(b"someone else's bytes"),
    }]
    assert remote.files()[key] == b"someone else's bytes", "nothing is uploaded over it"
    rows = _rows(db_session)
    assert rows[(img.id, "capture")].refused_at is not None
    assert rows[(img.id, "capture")].verified_at is None
    assert rows[(other.id, "capture")].verified_at is not None, "the run goes on to the next object"
    logged = db_session.query(SystemLog).filter(SystemLog.action == "image_upload_refused").one()
    assert _sha1(b"local bytes") in logged.detail and _sha1(b"someone else's bytes") in logged.detail

    copies = len(remote.calls("copyto"))
    _run(db_session)
    assert len(remote.calls("copyto")) == copies, "a refused object is not retried"


@pytest.mark.unit
def test_an_unanswered_listing_stops_the_run_and_marks_nothing(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    _image(db_session, vol, override_projects_root, "f2.jpg", b"two")
    remote.set_modes("listing-refused")

    result = _run(db_session)

    assert result["status"] == "aborted"
    assert "401" in result["error"]
    assert remote.calls("copyto") == [], "an unanswered listing is never read as an absent object"
    assert remote.files() == {}
    assert _rows(db_session) == {}
    assert len(remote.calls("lsjson")) == 1, "the run stops at the first unanswered listing"


@pytest.mark.unit
def test_not_found_is_absence_only_when_the_parent_listing_agrees(remote, db_session, override_projects_root, monkeypatch):
    """rclone says the directory is not found while its parent lists it: not an answer."""
    import app.core.image_upload as iu
    from app.core.catalogue_backup import VerificationUnavailable

    remote.put(f"{TENANT}/{STEM}/captures/x.jpg", b"x")
    real = iu.lsjson_rows

    def lying(path, **kw):
        if path.rstrip("/").endswith("/captures"):
            return None
        return real(path, **kw)

    monkeypatch.setattr(iu, "lsjson_rows", lying)
    with pytest.raises(VerificationUnavailable):
        iu.listed_object(f"{TENANT}/{STEM}/captures/x.jpg")
    # A directory that is really absent is absent.
    monkeypatch.setattr(iu, "lsjson_rows", real)
    assert iu.listed_object(f"{TENANT}/other/captures/x.jpg").present is False


@pytest.mark.unit
def test_a_missing_remote_root_is_not_an_answer(remote, db_session, override_projects_root, monkeypatch):
    from app.core import config
    from app.core.catalogue_backup import VerificationUnavailable
    from app.core.image_upload import listed_object

    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", str(remote.root / "no-such-bucket"))
    with pytest.raises(VerificationUnavailable):
        listed_object(f"{TENANT}/{STEM}/captures/x.jpg")


@pytest.mark.unit
def test_a_failed_copy_stops_the_run_and_marks_nothing(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    _image(db_session, vol, override_projects_root, "f2.jpg", b"two")
    remote.set_modes("copy-fails")
    result = _run(db_session)
    assert result["status"] == "failed"
    assert len(remote.calls("copyto")) == 1
    assert _rows(db_session) == {}


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_listing_without_the_sha1_after_upload_is_not_verified(remote, db_session, override_projects_root):
    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    remote.set_modes("strip-hashes")

    result = _run(db_session)

    assert result["unverified"] == 1 and result["uploaded"] == 0
    row = _rows(db_session)[(img.id, "capture")]
    assert row.uploaded_at is not None and row.verified_at is None


@pytest.mark.unit
def test_a_listed_object_without_a_sha1_before_upload_is_neither_uploaded_nor_verified(
        remote, db_session, override_projects_root):
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    remote.put(f"{TENANT}/{STEM}/captures/f1.jpg", b"one")
    remote.set_modes("strip-hashes", "strip-always")
    result = _run(db_session)
    assert remote.calls("copyto") == []
    assert result["skipped"][0]["reason"] == "listed with no SHA1"
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_a_crash_between_upload_and_verification_is_verified_by_the_next_run(
        remote, db_session, override_projects_root):
    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    remote.set_modes("refuse-after-copy")

    first = _run(db_session)
    assert first["status"] == "aborted"
    row = _rows(db_session)[(img.id, "capture")]
    assert row.uploaded_at is not None and row.verified_at is None, "uploaded, not marked"

    remote.set_modes()
    second = _run(db_session)
    assert second["already_present"] == 1
    assert len(remote.calls("copyto")) == 1, "verified from the listing, not uploaded again"
    assert _rows(db_session)[(img.id, "capture")].verified_at is not None


@pytest.mark.unit
def test_an_upload_that_never_arrived_is_uploaded_again_to_the_frozen_key(
        remote, db_session, override_projects_root):
    from app.models.record import RecordImage

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    remote.set_modes("refuse-after-copy")
    _run(db_session)
    key = f"{TENANT}/{STEM}/captures/f1.jpg"
    (remote.root / key).unlink()

    # Renamed on disk before the next run: the key stays the one stored.
    old = Path(img.file_path)
    new = old.with_name(f"{STEM}-img_0001_c1.jpg")
    old.rename(new)
    db_session.query(RecordImage).filter(RecordImage.id == img.id).update({"file_path": str(new), "filename": new.name})
    db_session.commit()

    remote.set_modes()
    result = _run(db_session)
    assert result["uploaded"] == 1
    assert remote.files() == {key: b"one"}
    assert _rows(db_session)[(img.id, "capture")].object_key == key


# ---------------------------------------------------------------------------
# Frozen keys
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_key_is_unchanged_after_a_move_on_disk_and_a_renumber(remote, db_session, override_projects_root):
    from app.core.image_upload import volume_upload_state
    from app.models.record import RecordImage

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.CR2", b"raw", preview=b"prev")
    _run(db_session)
    keys = {k: r.object_key for k, r in _rows(db_session).items()}

    # Renumbered: the master and its sidecar get new names.
    old = Path(img.file_path)
    new = old.with_name(f"{STEM}-img_0007_c1.CR2")
    old.rename(new)
    from app.core.paths import preview_sidecar_of
    preview_sidecar_of(old).rename(preview_sidecar_of(new))
    db_session.query(RecordImage).filter(RecordImage.id == img.id).update({"file_path": str(new), "filename": new.name})
    db_session.commit()

    calls = len(remote.calls())
    _run(db_session)
    assert remote.calls()[calls:] == []
    assert {k: r.object_key for k, r in _rows(db_session).items()} == keys
    assert volume_upload_state(db_session, vol)["received"] is True


@pytest.mark.unit
def test_a_stored_key_cannot_be_changed(remote, db_session, override_projects_root):
    from app.models.record import ImmutableFieldError

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.jpg", b"j")
    _run(db_session)
    row = _rows(db_session)[(img.id, "capture")]
    row.object_key = "elsewhere/f1.jpg"
    with pytest.raises(ImmutableFieldError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------

def _adoption_fixture(db, root, remote):
    vol = _volume(db)
    a = _image(db, vol, root, "a.CR2", b"raw a", preview=b"prev a")
    b = _image(db, vol, root, "b.CR2", b"raw b")
    c = _image(db, vol, root, "c.jpg", b"jpeg c")
    d1 = _image(db, vol, root, "d1.jpg", b"twin")
    d2 = _image(db, vol, root, "d2.jpg", b"twin")
    # An older layout, not the contract's.
    remote.put("ahr/tomo-6/images/a.CR2", b"raw a")
    remote.put("ahr/tomo-6/images/a_preview.jpg", b"prev a")
    remote.put("ahr/tomo-6/images/b.CR2", b"raw b")
    remote.put("ahr/tomo-6/copia/b.CR2", b"raw b")   # two remote objects with one SHA1
    remote.put("ahr/tomo-6/images/d.jpg", b"twin")   # one remote object, two local images
    return a, b, c, d1, d2


@pytest.mark.unit
def test_adoption_without_apply_writes_nothing(remote, db_session, override_projects_root):
    from app.core.image_upload import adopt_objects

    _adoption_fixture(db_session, override_projects_root, remote)
    report = adopt_objects(db_session, "ahr/tomo-6")
    assert len(report["adopted"]) == 2 and report["applied"] is False
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_adoption_records_unique_matches_and_reports_the_rest(remote, db_session, override_projects_root):
    from app.core.image_upload import adopt_objects

    a, b, c, d1, d2 = _adoption_fixture(db_session, override_projects_root, remote)
    before = remote.files()

    report = adopt_objects(db_session, "/ahr/tomo-6/", apply=True)

    assert {(e["image_id"], e["kind"], e["key"]) for e in report["adopted"]} == {
        (a.id, "capture", "ahr/tomo-6/images/a.CR2"),
        (a.id, "preview", "ahr/tomo-6/images/a_preview.jpg"),
    }
    assert [e["image_id"] for e in report["ambiguous_remote"]] == [b.id]
    assert report["ambiguous_remote"][0]["keys"] == ["ahr/tomo-6/copia/b.CR2", "ahr/tomo-6/images/b.CR2"]
    assert report["ambiguous_local"] == [{"key": "ahr/tomo-6/images/d.jpg", "images": [
        {"image_id": d1.id, "kind": "capture", "local_path": str(Path(d1.file_path).resolve())},
        {"image_id": d2.id, "kind": "capture", "local_path": str(Path(d2.file_path).resolve())},
    ]}]
    assert report["unmatched"] == 1  # c.jpg
    assert remote.calls("copyto") == []
    assert remote.files() == before, "adoption changes nothing at the destination"

    rows = _rows(db_session)
    assert set(rows) == {(a.id, "capture"), (a.id, "preview")}
    for row in rows.values():
        assert row.verified_at is not None and row.uploaded_at is None

    # The next run uploads the rest under the contract's keys and leaves the adopted alone.
    result = _run(db_session)
    rows = _rows(db_session)
    assert rows[(a.id, "capture")].object_key == "ahr/tomo-6/images/a.CR2"
    assert rows[(c.id, "capture")].object_key == f"{TENANT}/{STEM}/captures/c.jpg"
    assert not [call for call in remote.calls("copyto") if "a.CR2" in call[1]]
    assert result["status"] == "done"


@pytest.mark.unit
def test_adoption_of_an_unanswered_listing_raises(remote, db_session, override_projects_root):
    from app.core.catalogue_backup import VerificationUnavailable
    from app.core.image_upload import adopt_objects

    _adoption_fixture(db_session, override_projects_root, remote)
    remote.set_modes("listing-refused")
    with pytest.raises(VerificationUnavailable):
        adopt_objects(db_session, "ahr/tomo-6", apply=True)
    assert _rows(db_session) == {}


@pytest.mark.unit
def test_the_adopt_command_reports_as_json(remote, db_session, override_projects_root, capsys):
    from app.core import image_upload

    _adoption_fixture(db_session, override_projects_root, remote)
    assert image_upload.main(["adopt", "ahr/tomo-6"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] is False and len(out["adopted"]) == 2


# ---------------------------------------------------------------------------
# The received indicator
# ---------------------------------------------------------------------------

def _as(role):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username=role, email=f"{role}@example.com", hashed_password="x", role=role, is_active=True)


@pytest.mark.unit
def test_the_volume_route_reports_counts_and_received(remote, client, db_session, override_projects_root):
    _as("reviewer")
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.CR2", b"raw", preview=b"prev")
    refused = _image(db_session, vol, override_projects_root, "f2.jpg", b"mine")
    remote.put(f"{TENANT}/{STEM}/captures/f2.jpg", b"theirs")

    before = client.get(f"/collections/{vol.id}/offsite").json()
    assert (before["total"], before["verified"], before["pending"], before["refused"]) == (3, 0, 3, 0)
    assert before["received"] is False and before["last_verified_at"] is None

    _run(db_session)
    after = client.get(f"/collections/{vol.id}/offsite").json()
    assert (after["total"], after["verified"], after["pending"], after["refused"]) == (3, 2, 0, 1)
    assert after["received"] is False
    assert after["last_verified_at"] is not None
    assert after["refusals"][0]["image_id"] == refused.id
    assert after["refusals"][0]["remote_sha1"] == _sha1(b"theirs")


@pytest.mark.unit
def test_a_volume_is_received_when_every_object_is_verified(remote, client, db_session, override_projects_root):
    _as("operator")
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.CR2", b"raw", preview=b"prev")
    empty = _volume(db_session, name="Vacío", stem="xx-test-002")
    nostem = _volume(db_session, name="Sin raíz", stem=None)
    _run(db_session)

    assert client.get(f"/collections/{vol.id}/offsite").json()["received"] is True
    body = client.get(f"/collections/{empty.id}/offsite").json()
    assert body["total"] == 0 and body["received"] is False
    body = client.get(f"/collections/{nostem.id}/offsite").json()
    assert body["uploadable"] is False and body["received"] is False
    assert client.get("/collections/999999/offsite").status_code == 404


@pytest.mark.unit
def test_the_unit_summary_is_on_the_offsite_status_route(remote, client, db_session, override_projects_root):
    _as("reviewer")
    vol = _volume(db_session)
    _image(db_session, vol, override_projects_root, "f1.jpg", b"one")
    vol2 = _volume(db_session, name="Tomo 2", stem="xx-test-002")
    _image(db_session, vol2, override_projects_root, "f2.jpg", b"two")
    remote.put(f"{TENANT}/xx-test-002/captures/f2.jpg", b"other")
    _run(db_session)

    images = client.get("/system/catalogue-backup").json()["images"]
    assert images["enabled"] is True and images["tenant"] == TENANT
    assert (images["volumes"], images["volumes_received"]) == (2, 1)
    assert (images["total"], images["verified"], images["pending"], images["refused"]) == (2, 1, 0, 1)
    assert images["last_verified_at"] is not None


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_staging_links_the_file_and_the_sweep_clears_leftovers(remote, db_session, override_projects_root):
    from app.core.image_upload import STAGING_DIR_NAME, _stage, sweep_staging

    src = override_projects_root / "f.jpg"
    src.write_bytes(b"x")
    staged = _stage(src)
    assert staged.linked and staged.path.stat().st_ino == src.stat().st_ino
    # A rename of the original does not change what the link reads.
    src.rename(src.with_name("g.jpg"))
    assert staged.path.read_bytes() == b"x"
    sweep_staging()
    assert not (override_projects_root / STAGING_DIR_NAME).exists()


@pytest.mark.unit
def test_a_file_that_changes_while_it_is_hashed_is_skipped(tmp_path, monkeypatch):
    import app.core.image_upload as iu

    f = tmp_path / "f.jpg"
    f.write_bytes(b"x" * 10)
    real_stat = Path.stat
    calls = {"n": 0}

    def stat_then_grow(self, *a, **kw):
        result = real_stat(self, *a, **kw)
        calls["n"] += 1
        if calls["n"] == 1:
            with open(self, "ab") as fh:
                fh.write(b"more")
        return result

    monkeypatch.setattr(Path, "stat", stat_then_grow)
    assert iu.stable_sha1(f) is None


@pytest.mark.unit
def test_an_adopted_key_may_name_the_file_otherwise_than_the_disk(remote, db_session, override_projects_root):
    """Contract names written in the bucket for images that keep older names on the card."""
    from app.core.image_upload import adopt_objects, volume_upload_state
    from app.models.record import RecordImage

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "IMG_0001.CR2", b"raw", preview=b"prev")
    cap_key = f"{TENANT}/{STEM}/captures/{STEM}-img_0001_c1.CR2"
    prev_key = f"{TENANT}/{STEM}/previews/{STEM}-img_0001_c1_preview.jpg"
    remote.put(cap_key, b"raw")
    remote.put(prev_key, b"prev")

    report = adopt_objects(db_session, f"{TENANT}/{STEM}", apply=True)
    assert {e["key"] for e in report["adopted"]} == {cap_key, prev_key}

    # The disk name stays what it was; nothing is uploaded under it.
    calls = len(remote.calls())
    result = _run(db_session)
    assert result["uploaded"] == 0 and remote.calls()[calls:] == []
    assert sorted(remote.files()) == sorted([cap_key, prev_key])
    rows = _rows(db_session)
    assert rows[(img.id, "capture")].object_key == cap_key
    assert Path(db_session.get(RecordImage, img.id).file_path).name == "IMG_0001.CR2"
    assert volume_upload_state(db_session, vol)["received"] is True


# ---------------------------------------------------------------------------
# A renumber or a move between the work list and the copy
# ---------------------------------------------------------------------------

def _swap_names(db, a, b):
    """What a renumber that swaps two images' numbers leaves: files swapped on disk, rows committed."""
    from app.models.record import RecordImage

    pa, pb = Path(a.file_path), Path(b.file_path)
    tmp = pa.with_name("swap.tmp")
    pa.rename(tmp)
    pb.rename(pa)
    tmp.rename(pb)
    db.query(RecordImage).filter(RecordImage.id == a.id).update({"file_path": str(pb), "filename": pb.name})
    db.query(RecordImage).filter(RecordImage.id == b.id).update({"file_path": str(pa), "filename": pa.name})
    db.commit()


@pytest.mark.unit
def test_a_renumber_after_the_work_list_is_built_uploads_each_image_its_own_bytes(
        remote, db_session, override_projects_root, monkeypatch):
    import app.core.image_upload as iu

    vol = _volume(db_session)
    a = _image(db_session, vol, override_projects_root, f"{STEM}-img_0001_c1.jpg", b"AAAA", thumbnail=False)
    b = _image(db_session, vol, override_projects_root, f"{STEM}-img_0002_c1.jpg", b"BBBB", thumbnail=False)
    real = iu.pending_work

    def then_renumber(db):
        work = real(db)
        _swap_names(db, a, b)
        return work

    monkeypatch.setattr(iu, "pending_work", then_renumber)
    _run(db_session)
    rows = _rows(db_session)
    bucket = remote.files()
    for image, data in ((a, b"AAAA"), (b, b"BBBB")):
        row = rows[(image.id, "capture")]
        assert row.local_sha1 == _sha1(data)
        assert bucket[row.object_key] == data


@pytest.mark.unit
def test_the_read_hold_and_the_file_change_hold_exclude_each_other():
    from fastapi import HTTPException

    from app.core.export_jobs import ExportJobRegistry, FilesChanging

    jobs = ExportJobRegistry()
    with jobs.hold_for_reading([1]):
        with pytest.raises(HTTPException) as refused:
            with jobs.hold_for_file_changes([1]):
                pass
        assert refused.value.status_code == 409
        with jobs.hold_for_file_changes([2]):
            pass  # another volume is not held
    with jobs.hold_for_file_changes([1]):
        with pytest.raises(FilesChanging):
            with jobs.hold_for_reading([1]):
                pass
    with jobs.hold_for_file_changes([1]):
        pass  # both released


@pytest.mark.unit
def test_an_image_whose_volume_is_held_for_file_changes_is_skipped_for_the_run(
        remote, db_session, override_projects_root):
    from app.core.export_jobs import export_jobs

    vol = _volume(db_session)
    img = _image(db_session, vol, override_projects_root, "f1.jpg", b"one", thumbnail=False)
    with export_jobs.hold_for_file_changes([vol.id]):
        result = _run(db_session)
    assert result["skipped"] == [{"image_id": img.id, "kind": "capture",
                                  "reason": "files of the volume being changed"}]
    assert remote.calls("copyto") == [] and _rows(db_session) == {}
    assert _run(db_session)["uploaded"] == 1


def _manifest(root, stem, cid, rel, data):
    m = root / stem / "metadata" / "manifest.jsonl"
    m.parent.mkdir(parents=True, exist_ok=True)
    with m.open("a") as fh:
        fh.write(json.dumps({"capture_id": cid, "files": [
            {"relative_path": rel, "sha256": hashlib.sha256(data).hexdigest()}]}) + "\n")


@pytest.mark.unit
def test_a_capture_whose_bytes_are_not_its_capture_records_is_refused(remote, db_session, override_projects_root):
    from app.models.record import RecordImage

    vol = _volume(db_session)
    good = _image(db_session, vol, override_projects_root, "f1.jpg", b"as captured", thumbnail=False)
    bad = _image(db_session, vol, override_projects_root, "f2.jpg", b"changed since", thumbnail=False)
    for img, cid, data in ((good, "c-1", b"as captured"), (bad, "c-2", b"as captured too")):
        db_session.query(RecordImage).filter(RecordImage.id == img.id).update({"capture_id": cid})
        _manifest(override_projects_root, STEM, cid, f"captures/{Path(img.file_path).name}", data)
    db_session.commit()

    result = _run(db_session)

    assert result["uploaded"] == 1
    [refusal] = result["refused"]
    assert refusal["reason"] == "fixity" and refusal["image_id"] == bad.id
    assert refusal["capture_sha256"] == hashlib.sha256(b"as captured too").hexdigest()
    assert refusal["local_sha256"] == hashlib.sha256(b"changed since").hexdigest()
    assert f"{TENANT}/{STEM}/captures/f2.jpg" not in remote.files()
    row = _rows(db_session)[(bad.id, "capture")]
    assert row.refusal == "fixity" and row.refused_at is not None
