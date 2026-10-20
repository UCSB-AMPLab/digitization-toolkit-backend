"""Deleting a record or an image takes its `_preview.jpg` sidecar with it (NEH-257).

A RAW capture lands on the card as two files: the master (`IMG_0001.CR2`) and a
JPEG sidecar the RAW backend writes beside it (`IMG_0001_preview.jpg`), which is
what the review UI and the thumbnail generator can actually read. Both deletion
endpoints unlink the master and the thumbnail and leave the sidecar, so every
deleted capture costs about 2.3 MB on the card for good. On the Rionegro unit
that comes to 2,403 orphaned previews and 5.5 GB on a 115 GB card, which reached
100% on 14 October and stopped capture.

The orphans are also unreadable as evidence: a preview with no master and no
database row looks, on the filesystem, exactly like a master that was lost.

What these tests assert is the file's absence from disk after the request, not
that some function was called. The rule under test is positional, not
format-sensitive: what goes is the file named `<master stem>_preview.jpg` in the
master's own directory, which is the rule `capture/service.py`'s rollback path
already applies. Nothing else in the directory is touched, and a master whose
stored path escapes the storage roots loses neither master nor sidecar.

Deriving the name from the stem means two masters of one frame that differ only
in extension name the same sidecar, so the single-image delete has to leave a
sidecar a surviving row still points at. The record delete has no such
survivor and keeps deleting everything. Both halves are asserted below,
against pairs that share a stem rather than pairs that merely share a
directory — the distinct-stem case passes whatever the handler does with the
siblings, so on its own it does not test the property it names.

Every file these tests create lives under the per-test temporary projects root
from the `override_projects_root` fixture. `_must_be_inside_storage` asserts that
before any delete runs, so a misconfigured root fails the test rather than
pointing a delete at a real capture tree.
"""

import pytest


def _user(username, role):
    from app.models.user import User

    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    import app.api.records as records

    user = _user(username, role)
    for dep in (records.allow_contributor, records.allow_read_only):
        app.dependency_overrides[dep] = lambda u=user: u
    return client


@pytest.fixture
def capture_dir(override_projects_root):
    """Where masters and their sidecars sit: `<project>/images/main`."""
    d = override_projects_root / "sidecar_project" / "images" / "main"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def thumbs_dir(override_projects_root):
    """Where the generated thumbnail sits: `<project>/images/thumbnails`."""
    d = override_projects_root / "sidecar_project" / "images" / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _must_be_inside_storage(*paths):
    """Refuse to run a deletion test against anything outside the storage roots."""
    from app.core.paths import resolve_within_storage

    for p in paths:
        assert resolve_within_storage(str(p)) is not None, (
            f"test fixture {p} is not inside the configured storage roots; "
            f"refusing to point a delete at it"
        )


def _capture(capture_dir, thumbs_dir, stem, suffix=".CR2", sidecar=True):
    """Write a capture on disk: master, its `_preview.jpg` sidecar, and a thumbnail."""
    master = capture_dir / f"{stem}{suffix}"
    master.write_bytes(b"master-bytes")
    preview = capture_dir / f"{stem}_preview.jpg"
    if sidecar:
        preview.write_bytes(b"preview-bytes")
    thumb = thumbs_dir / f"{stem}_thumb.jpg"
    thumb.write_bytes(b"thumb-bytes")
    return master, preview, thumb


def _shared_stem_pair(capture_dir, thumbs_dir, stem):
    """Two masters of one frame differing only in extension, over one sidecar.

    A multi-format capture writes both a JPEG and a RAW master for the same
    frame, and the RAW backend writes one `<stem>_preview.jpg` beside them.
    Each row gets its own thumbnail, so a deleted thumbnail cannot be mistaken
    for the shared sidecar.
    """
    jpeg = capture_dir / f"{stem}.jpg"
    jpeg.write_bytes(b"jpeg-master-bytes")
    raw = capture_dir / f"{stem}.CR2"
    raw.write_bytes(b"raw-master-bytes")
    preview = capture_dir / f"{stem}_preview.jpg"
    preview.write_bytes(b"preview-bytes")
    jpeg_thumb = thumbs_dir / f"{stem}_jpg_thumb.jpg"
    jpeg_thumb.write_bytes(b"thumb-bytes")
    raw_thumb = thumbs_dir / f"{stem}_raw_thumb.jpg"
    raw_thumb.write_bytes(b"thumb-bytes")
    return jpeg, jpeg_thumb, raw, raw_thumb, preview


def _record(db_session, status="rejected", capture_mode="single"):
    from app.models.record import Record

    rec = Record(title="r", status=status, capture_mode=capture_mode)
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
        thumbnail_path=str(thumb) if thumb is not None else None,
        format=master.suffix.lstrip(".").lower(),
        is_current=is_current,
    )
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)
    return img


# ---------------------------------------------------------------------------
# DELETE /records/{rec_id}
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_deleting_a_record_removes_the_preview_sidecar(client, db_session, capture_dir, thumbs_dir):
    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_0001")
    _must_be_inside_storage(master, preview)
    rec = _record(db_session)
    _image(db_session, rec, master, thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/{rec.id}")

    assert resp.status_code == 200, resp.text
    assert not preview.exists(), f"{preview.name} survived the record delete"


@pytest.mark.unit
def test_deleting_a_record_removes_the_sidecar_of_every_image(client, db_session, capture_dir, thumbs_dir):
    """A dual-mode record holds a pair; both sidecars go, not just the first."""
    left = _capture(capture_dir, thumbs_dir, "IMG_0002_L")
    right = _capture(capture_dir, thumbs_dir, "IMG_0002_R")
    _must_be_inside_storage(left[0], left[1], right[0], right[1])
    rec = _record(db_session, capture_mode="dual")
    _image(db_session, rec, left[0], left[2])
    _image(db_session, rec, right[0], right[2])

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/{rec.id}")

    assert resp.status_code == 200, resp.text
    assert not left[1].exists(), f"{left[1].name} survived the record delete"
    assert not right[1].exists(), f"{right[1].name} survived the record delete"


@pytest.mark.unit
def test_deleting_a_record_removes_a_superseded_images_sidecar(client, db_session, capture_dir, thumbs_dir):
    """A superseded image is still a file on the card, and its sidecar goes too.

    A rejected-and-recaptured image stays in the database with `is_current`
    False (NEH-208), so a record carries the files of every take it ever had.
    Deleting the record has to clear all of them.
    """
    old = _capture(capture_dir, thumbs_dir, "IMG_0003_take1")
    new = _capture(capture_dir, thumbs_dir, "IMG_0003_take2")
    _must_be_inside_storage(old[0], old[1], new[0], new[1])
    rec = _record(db_session)
    _image(db_session, rec, old[0], old[2], is_current=False)
    _image(db_session, rec, new[0], new[2])

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/{rec.id}")

    assert resp.status_code == 200, resp.text
    assert not old[1].exists(), f"{old[1].name} (superseded) survived the record delete"
    assert not new[1].exists(), f"{new[1].name} survived the record delete"


@pytest.mark.unit
def test_deleting_a_record_still_removes_the_master_and_the_thumbnail(client, db_session, capture_dir, thumbs_dir):
    """Already true before NEH-257; asserted so the sidecar fix cannot cost it."""
    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_0004")
    _must_be_inside_storage(master, preview, thumb)
    rec = _record(db_session)
    _image(db_session, rec, master, thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/{rec.id}")

    assert resp.status_code == 200, resp.text
    assert not master.exists(), "the master survived the record delete"
    assert not thumb.exists(), "the thumbnail survived the record delete"


# ---------------------------------------------------------------------------
# DELETE /records/images/{img_id}
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_deleting_an_image_removes_the_preview_sidecar(client, db_session, capture_dir, thumbs_dir):
    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_0005")
    _must_be_inside_storage(master, preview)
    rec = _record(db_session)
    img = _image(db_session, rec, master, thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{img.id}")

    assert resp.status_code == 200, resp.text
    assert not preview.exists(), f"{preview.name} survived the image delete"


@pytest.mark.unit
def test_deleting_an_image_still_removes_the_master_and_the_thumbnail(client, db_session, capture_dir, thumbs_dir):
    """Already true before NEH-257; asserted so the sidecar fix cannot cost it."""
    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_0006")
    _must_be_inside_storage(master, preview, thumb)
    rec = _record(db_session)
    img = _image(db_session, rec, master, thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{img.id}")

    assert resp.status_code == 200, resp.text
    assert not master.exists(), "the master survived the image delete"
    assert not thumb.exists(), "the thumbnail survived the image delete"


@pytest.mark.unit
def test_deleting_one_image_leaves_a_siblings_sidecar_under_a_distinct_stem(client, db_session, capture_dir, thumbs_dir):
    """The sidecar that goes is the deleted master's own, not the directory's.

    Guards against a fix that globs `*_preview.jpg` beside the master: the
    second half of a dual pair lives in the same directory, carries its own
    stem and its own sidecar, and is still referenced.

    This is the easy half of the property. Distinct stems mean distinct sidecar
    names, so the survivor's file is never the one named for the deleted
    master. The hard half — two masters that share a stem and therefore share
    one sidecar — is the test below.
    """
    left = _capture(capture_dir, thumbs_dir, "IMG_0007_L")
    right = _capture(capture_dir, thumbs_dir, "IMG_0007_R")
    _must_be_inside_storage(left[0], left[1], right[0], right[1])
    rec = _record(db_session, capture_mode="dual")
    left_img = _image(db_session, rec, left[0], left[2])
    _image(db_session, rec, right[0], right[2])

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{left_img.id}")

    assert resp.status_code == 200, resp.text
    assert not left[1].exists(), f"{left[1].name} survived the image delete"
    assert right[0].exists(), "the sibling's master was deleted"
    assert right[1].exists(), f"{right[1].name} was deleted with its sibling"


@pytest.mark.unit
def test_deleting_one_of_two_masters_that_share_a_stem_leaves_the_shared_sidecar(client, db_session, capture_dir, thumbs_dir):
    """Masters differing only in extension share one sidecar, and the survivor needs it.

    A multi-format capture of one frame produces `folio.jpg` and `folio.CR2`
    and a single `folio_preview.jpg`. The sidecar's name is derived from the
    master's stem, so both rows derive the same name, and an unconditional
    unlink on either delete takes the preview the other row still points at.
    What is left behind then looks exactly like a capture whose preview was
    never written.
    """
    jpeg, jpeg_thumb, raw, raw_thumb, preview = _shared_stem_pair(
        capture_dir, thumbs_dir, "IMG_0011")
    _must_be_inside_storage(jpeg, raw, preview)
    rec = _record(db_session)
    jpeg_img = _image(db_session, rec, jpeg, jpeg_thumb)
    _image(db_session, rec, raw, raw_thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{jpeg_img.id}")

    assert resp.status_code == 200, resp.text
    assert not jpeg.exists(), "the deleted master survived"
    assert raw.exists(), "the surviving master was deleted"
    assert preview.exists(), (
        f"{preview.name} was deleted while {raw.name} still points at it")


@pytest.mark.unit
def test_a_shared_sidecar_is_protected_whichever_master_is_deleted(client, db_session, capture_dir, thumbs_dir):
    """The check cannot depend on which of the pair happens to be deleted first."""
    jpeg, jpeg_thumb, raw, raw_thumb, preview = _shared_stem_pair(
        capture_dir, thumbs_dir, "IMG_0012")
    _must_be_inside_storage(jpeg, raw, preview)
    rec = _record(db_session)
    _image(db_session, rec, jpeg, jpeg_thumb)
    raw_img = _image(db_session, rec, raw, raw_thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{raw_img.id}")

    assert resp.status_code == 200, resp.text
    assert not raw.exists(), "the deleted master survived"
    assert jpeg.exists(), "the surviving master was deleted"
    assert preview.exists(), (
        f"{preview.name} was deleted while {jpeg.name} still points at it")


@pytest.mark.unit
def test_a_superseded_sibling_also_protects_a_shared_sidecar(client, db_session, capture_dir, thumbs_dir):
    """A superseded row still points at its master, so its sidecar is still referenced.

    A rejected image is not deleted; it stays with `is_current` False
    (NEH-208) and remains queryable through GET /records/{id}/rejections,
    which is a view that reads previews. So what protects the sidecar is any
    surviving row sharing the stem, current or not — the same rule the record
    delete applies when it clears superseded files along with current ones.
    """
    jpeg, jpeg_thumb, raw, raw_thumb, preview = _shared_stem_pair(
        capture_dir, thumbs_dir, "IMG_0013")
    _must_be_inside_storage(jpeg, raw, preview)
    rec = _record(db_session)
    jpeg_img = _image(db_session, rec, jpeg, jpeg_thumb)
    _image(db_session, rec, raw, raw_thumb, is_current=False)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{jpeg_img.id}")

    assert resp.status_code == 200, resp.text
    assert raw.exists(), "a superseded master was deleted"
    assert preview.exists(), (
        f"{preview.name} was deleted while a superseded {raw.name} points at it")


@pytest.mark.unit
def test_deleting_the_last_master_of_a_shared_stem_removes_the_sidecar(client, db_session, capture_dir, thumbs_dir):
    """Protecting the sidecar must not turn into leaving it behind for good.

    Once no row points at a master sharing the stem, the preview is the orphan
    NEH-257 is about and it goes with the last delete.
    """
    jpeg, jpeg_thumb, raw, raw_thumb, preview = _shared_stem_pair(
        capture_dir, thumbs_dir, "IMG_0014")
    _must_be_inside_storage(jpeg, raw, preview)
    rec = _record(db_session)
    jpeg_img = _image(db_session, rec, jpeg, jpeg_thumb)
    raw_img = _image(db_session, rec, raw, raw_thumb)

    api = _client_as(client, "op", "operator")
    first = api.delete(f"/records/images/{jpeg_img.id}")
    assert first.status_code == 200, first.text
    assert preview.exists(), "the sidecar went with the first of the pair"

    second = api.delete(f"/records/images/{raw_img.id}")

    assert second.status_code == 200, second.text
    assert not raw.exists(), "the last master survived"
    assert not preview.exists(), f"{preview.name} outlived every row pointing at it"


@pytest.mark.unit
def test_deleting_a_record_removes_a_sidecar_two_of_its_masters_share(client, db_session, capture_dir, thumbs_dir):
    """The record delete takes every image, so the shared sidecar has no survivor.

    Asserted because the single-image delete now protects a sidecar a sibling
    still needs, and the record delete must not inherit that protection: there
    is nothing left to protect it for.
    """
    jpeg, jpeg_thumb, raw, raw_thumb, preview = _shared_stem_pair(
        capture_dir, thumbs_dir, "IMG_0015")
    _must_be_inside_storage(jpeg, raw, preview)
    rec = _record(db_session)
    _image(db_session, rec, jpeg, jpeg_thumb)
    _image(db_session, rec, raw, raw_thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/{rec.id}")

    assert resp.status_code == 200, resp.text
    assert not jpeg.exists(), "a master survived the record delete"
    assert not raw.exists(), "a master survived the record delete"
    assert not preview.exists(), f"{preview.name} survived the record delete"


@pytest.mark.unit
def test_deleting_an_image_with_no_sidecar_still_succeeds(client, db_session, capture_dir, thumbs_dir):
    """Most captures are plain JPEGs with no sidecar; a missing one is not an error."""
    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_0008", suffix=".jpg", sidecar=False)
    _must_be_inside_storage(master, thumb)
    assert not preview.exists()
    rec = _record(db_session)
    img = _image(db_session, rec, master, thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{img.id}")

    assert resp.status_code == 200, resp.text
    assert not master.exists(), "the master survived the image delete"


@pytest.mark.unit
def test_a_sidecar_beside_a_jpeg_master_goes_too(client, db_session, capture_dir, thumbs_dir):
    """The rule is the name beside the master, not the master's format.

    `capture/service.py`'s rollback applies it to every file of a capture
    without inspecting the suffix, and a JPEG master that happens to carry a
    `_preview.jpg` beside it is an orphan in exactly the same way. Checking the
    suffix instead would leave it, and the suffix list would then be a second
    place to keep in step with the backends.
    """
    master, preview, thumb = _capture(capture_dir, thumbs_dir, "IMG_0009", suffix=".jpg")
    _must_be_inside_storage(master, preview)
    rec = _record(db_session)
    img = _image(db_session, rec, master, thumb)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{img.id}")

    assert resp.status_code == 200, resp.text
    assert not preview.exists(), f"{preview.name} survived the image delete"


@pytest.mark.unit
def test_an_image_whose_path_escapes_storage_loses_no_sidecar(client, db_session, tmp_path, thumbs_dir):
    """Outside the storage roots nothing is unlinked, sidecar included.

    `resolve_within_storage` is what stops a stored path from reaching the
    filesystem. The sidecar has to be derived from the resolved path, not from
    the raw database string, or the fix would hand the delete endpoints a way
    out of the storage roots that the master unlink does not have.
    """
    from app.core.paths import resolve_within_storage

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    master = outside / "IMG_0010.CR2"
    master.write_bytes(b"master-bytes")
    preview = outside / "IMG_0010_preview.jpg"
    preview.write_bytes(b"preview-bytes")
    assert resolve_within_storage(str(master)) is None, "fixture is not outside storage"

    rec = _record(db_session)
    img = _image(db_session, rec, master, None)

    api = _client_as(client, "op", "operator")
    resp = api.delete(f"/records/images/{img.id}")

    assert resp.status_code == 200, resp.text
    assert master.exists(), "a master outside the storage roots was unlinked"
    assert preview.exists(), "a sidecar outside the storage roots was unlinked"
