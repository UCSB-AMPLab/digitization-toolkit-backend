#!/usr/bin/env python3
"""NEH-260: a record may leave `captured` only if the work is there to hand over.

The invariant: at least one current image, and no current image carrying a
`rejection_id`. The second clause is the substantive one — `rejection_id` is
written by the reject endpoint on the images current at that moment, and a
retake supersedes those while the replacements carry none, so a marked current
image means the rejection has not been answered. The first closes a vacuous
pass: delete every current image and a purely negative predicate holds on a
record with nothing in it.

It is checked in `_apply_status_change`, which all three promotion paths go
through, so every test here runs against all three. The paths do not report a
refusal the same way: the bulk endpoint skips a record it cannot move and
returns 200 without it, by design, while the other two refuse with a 422.
A volume whose record still has its rejected image current is a volume with
a recapture pending, which the collection path refuses first, with a 409
(app.core.recapture_turn; tests/unit/test_recapture_turn.py).

Run with: python -m pytest tests/unit/test_promotion_invariant.py

Version: 0.2.0
"""

import pytest


PATHS = ["patch", "bulk", "collection"]


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _collection(db_session):
    from app.models.project import Project
    from app.models.collection import Collection
    proj = Project(name="P")
    db_session.add(proj)
    db_session.commit()
    col = Collection(name="Tomo 1", project_id=proj.id)
    db_session.add(col)
    db_session.commit()
    db_session.refresh(col)
    return col


def _record(db_session, col, status="in_review", capture_mode="single"):
    from app.models.record import Record
    rec = Record(title="folio", collection_id=col.id, status=status,
                 capture_mode=capture_mode)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    return rec


def _image(db_session, rec, name="f1.jpg", is_current=True):
    from app.models.record import RecordImage
    img = RecordImage(record_id=rec.id, filename=name, file_path=f"/{name}",
                      format="jpg", role="single", is_current=is_current)
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)
    return img


def _reject_then_prepare_for_retake(client, db_session, rec):
    """Take a record through the real endpoints to `captured` with a marked image.

    The reject endpoint is what writes `rejection_id`, so the fixture goes
    through it rather than setting the column by hand.
    """
    rev = _client_as(client, "rev", "reviewer")
    resp = rev.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    op = _client_as(client, "op", "operator")
    resp = op.patch(f"/records/{rec.id}/status", json={"status": "captured"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "captured"
    db_session.refresh(rec)
    return op


def _promote(api, path, rec, col):
    if path == "patch":
        return api.patch(f"/records/{rec.id}/status", json={"status": "in_review"})
    if path == "bulk":
        return api.post("/records/bulk-status",
                        json={"record_ids": [rec.id], "status": "in_review"})
    return api.post(f"/collections/{col.id}/promote")


def _assert_promoted(resp, path, db_session, rec):
    assert resp.status_code == 200, resp.text
    if path == "bulk":
        assert rec.id in {r["id"] for r in resp.json()}, resp.text
    db_session.refresh(rec)
    assert rec.status == "in_review"


def _assert_refused(resp, path, db_session, rec, pending=False):
    if path == "bulk":
        # Documented behaviour: a record it cannot move is skipped, not an error.
        assert resp.status_code == 200, resp.text
        assert rec.id not in {r["id"] for r in resp.json()}, resp.text
    elif path == "collection" and pending:
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "recaptures_pending"
    else:
        assert resp.status_code == 422, resp.text
    db_session.refresh(rec)
    assert rec.status == "captured", "the record must not have moved"


# ---------------------------------------------------------------------------
# The four sequences, each through all three promotion paths
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("path", PATHS)
def test_retaken_record_can_be_handed_over(client, db_session, path):
    """Reject, retake with a proper supersede, promote — succeeds."""
    from datetime import datetime, timezone
    col = _collection(db_session)
    rec = _record(db_session, col)
    old = _image(db_session, rec, "old.jpg")
    op = _reject_then_prepare_for_retake(client, db_session, rec)

    # What the capture endpoints do on a retake: supersede, then add the
    # replacement, which carries no rejection_id.
    old.is_current = False
    old.superseded_at = datetime.now(timezone.utc)
    db_session.commit()
    _image(db_session, rec, "new.jpg")

    _assert_promoted(_promote(op, path, rec, col), path, db_session, rec)


@pytest.mark.unit
@pytest.mark.parametrize("path", PATHS)
def test_unretaken_record_cannot_be_handed_over(client, db_session, path):
    """Reject, promote without retaking — refused: the marked image is still current."""
    col = _collection(db_session)
    rec = _record(db_session, col)
    _image(db_session, rec, "old.jpg")
    op = _reject_then_prepare_for_retake(client, db_session, rec)

    _assert_refused(_promote(op, path, rec, col), path, db_session, rec, pending=True)


@pytest.mark.unit
@pytest.mark.parametrize("path", PATHS)
def test_emptied_record_cannot_be_handed_over(client, db_session, path):
    """Reject, delete every current image, promote — refused.

    This is the clause that keeps a purely negative test from passing on
    nothing: with no images at all, no image carries a rejection_id.
    """
    col = _collection(db_session)
    rec = _record(db_session, col)
    img = _image(db_session, rec, "old.jpg")
    op = _reject_then_prepare_for_retake(client, db_session, rec)

    resp = op.delete(f"/records/images/{img.id}")
    assert resp.status_code == 200, resp.text

    _assert_refused(_promote(op, path, rec, col), path, db_session, rec)


@pytest.mark.unit
@pytest.mark.parametrize("path", PATHS)
def test_added_image_without_superseding_does_not_release_the_record(client, db_session, path):
    """Reject, add an image without superseding, promote — refused.

    A replacement that leaves the rejected frame current has not answered the
    rejection, however many new images sit beside it.
    """
    col = _collection(db_session)
    rec = _record(db_session, col)
    _image(db_session, rec, "old.jpg")
    op = _reject_then_prepare_for_retake(client, db_session, rec)

    _image(db_session, rec, "extra.jpg")

    _assert_refused(_promote(op, path, rec, col), path, db_session, rec, pending=True)


# ---------------------------------------------------------------------------
# A record that was never rejected
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("path", PATHS)
def test_a_fresh_capture_can_be_handed_over(client, db_session, path):
    col = _collection(db_session)
    rec = _record(db_session, col, status="captured")
    _image(db_session, rec, "f1.jpg")
    op = _client_as(client, "op", "operator")

    _assert_promoted(_promote(op, path, rec, col), path, db_session, rec)


@pytest.mark.unit
@pytest.mark.parametrize("path", PATHS)
def test_a_record_with_no_image_at_all_cannot_be_handed_over(client, db_session, path):
    col = _collection(db_session)
    rec = _record(db_session, col, status="captured")
    op = _client_as(client, "op", "operator")

    _assert_refused(_promote(op, path, rec, col), path, db_session, rec)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
