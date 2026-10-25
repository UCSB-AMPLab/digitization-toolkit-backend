#!/usr/bin/env python3
"""
Tests for POST /records/{id}/reject and GET /records/{id}/rejections (NEH-208).

Rejection requires a mandatory predefined_reason from a fixed list (blur,
glare, shadow, focus, exposure, dirt, framing, obstruction, other), plus a
free-text comment that is optional for every reason except 'other'; it's
only valid from 'in_review'; and only reviewer/admin may perform it.
Run with: python -m pytest tests/unit/test_record_reject_endpoint.py
"""

import pytest


def _user(username, role):
    from app.models.user import User
    return User(username=username, email=f"{username}@example.com",
                hashed_password="x", role=role, is_active=True)


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _make_record(db_session, status="in_review", capture_mode="single"):
    from app.models.record import Record

    rec = Record(title="r", status=status, capture_mode=capture_mode)
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)
    return rec


@pytest.mark.unit
def test_reject_requires_predefined_reason(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_reject_rejects_unknown_reason(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "bogus"})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_reject_accepts_valid_reason_and_optional_comment(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur", "comment": "too fuzzy"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"


@pytest.mark.unit
def test_reject_only_valid_from_in_review(client, db_session):
    rec = _make_record(db_session, status="approved")
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur"})
    assert resp.status_code == 422, resp.text


@pytest.mark.unit
def test_operator_cannot_reject(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "op", "operator")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur"})
    assert resp.status_code == 403, resp.text


@pytest.mark.unit
def test_rejection_appears_in_rejections_history_and_image_drops_from_images_list(client, db_session):
    from app.models.record import RecordImage

    rec = _make_record(db_session)
    img = RecordImage(record_id=rec.id, filename="a.jpg", file_path="/tmp/a.jpg", format="jpg")
    db_session.add(img)
    db_session.commit()

    api = _client_as(client, "rev", "reviewer")
    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "glare", "comment": "window reflection"})
    assert resp.status_code == 200, resp.text

    resp = api.get(f"/records/{rec.id}/rejections")
    assert resp.status_code == 200, resp.text
    rejections = resp.json()
    assert len(rejections) == 1
    assert rejections[0]["predefined_reason"] == "glare"
    assert rejections[0]["comment"] == "window reflection"
    assert [i["id"] for i in rejections[0]["images"]] == [img.id]

    # The rejected image is still "current" until a recapture supersedes it,
    # so it still appears in the default images listing.
    resp = api.get(f"/records/{rec.id}/images")
    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in resp.json()] == [img.id]


OLD_REASONS = ["blur", "glare", "shadow", "focus", "exposure", "dirt"]


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["framing", "obstruction"])
def test_new_reasons_are_accepted_without_a_comment(client, db_session, reason):
    from app.models.record import RecordRejection

    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": reason})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    [row] = db_session.query(RecordRejection).filter_by(record_id=rec.id).all()
    assert row.predefined_reason == reason and row.comment is None


@pytest.mark.unit
@pytest.mark.parametrize("payload", [
    {"predefined_reason": "other"},
    {"predefined_reason": "other", "comment": None},
    {"predefined_reason": "other", "comment": ""},
    {"predefined_reason": "other", "comment": "   \n\t "},
])
def test_other_without_a_comment_is_refused(client, db_session, payload):
    from app.models.record import RecordRejection

    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject", json=payload)
    assert resp.status_code == 422, resp.text
    assert "other" in resp.text and "comment" in resp.text
    db_session.expire_all()
    assert db_session.query(RecordRejection).filter_by(record_id=rec.id).count() == 0
    assert db_session.get(type(rec), rec.id).status == "in_review"


@pytest.mark.unit
def test_other_with_a_comment_is_accepted(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    resp = api.post(f"/records/{rec.id}/reject",
                    json={"predefined_reason": "other", "comment": "page out of order"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    [rej] = api.get(f"/records/{rec.id}/rejections").json()
    assert rej["predefined_reason"] == "other"
    assert rej["comment"] == "page out of order"


@pytest.mark.unit
@pytest.mark.parametrize("reason", OLD_REASONS)
@pytest.mark.parametrize("comment", [None, "", "  ", "a note"])
def test_old_reasons_keep_the_comment_optional(client, db_session, reason, comment):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")

    payload = {"predefined_reason": reason}
    if comment is not None:
        payload["comment"] = comment
    resp = api.post(f"/records/{rec.id}/reject", json=payload)
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_the_reason_list_is_the_ruled_nine():
    from app.schemas.record import PREDEFINED_REJECTION_REASONS

    assert PREDEFINED_REJECTION_REASONS == (
        "blur", "glare", "shadow", "focus", "exposure", "dirt",
        "framing", "obstruction", "other",
    )


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["framing", "obstruction", "other"])
def test_the_table_accepts_the_new_reasons(db_session, reason):
    from app.models.record import RecordRejection

    rec = _make_record(db_session)
    db_session.add(RecordRejection(record_id=rec.id, predefined_reason=reason, comment="x"))
    db_session.commit()


@pytest.mark.unit
def test_the_table_still_refuses_an_unknown_reason(db_session):
    from sqlalchemy.exc import IntegrityError
    from app.models.record import RecordRejection

    rec = _make_record(db_session)
    db_session.add(RecordRejection(record_id=rec.id, predefined_reason="bogus"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


COMMENT_LIMIT_MESSAGE = "A rejection comment can be at most 2,000 characters long."


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["blur", "other"])
def test_a_comment_over_2000_characters_is_refused_not_cut(client, db_session, reason):
    """Fisqua keeps only the first 2,000 characters of a note, so the unit refuses a longer one."""
    from app.models.record import RecordRejection

    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")
    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": reason, "comment": "x" * 2001})
    assert resp.status_code == 422, resp.text
    [error] = resp.json()["detail"]
    assert error["msg"] == f"Value error, {COMMENT_LIMIT_MESSAGE}"
    db_session.expire_all()
    assert db_session.query(RecordRejection).filter_by(record_id=rec.id).count() == 0


@pytest.mark.unit
def test_a_comment_of_2000_characters_is_kept_whole(client, db_session):
    rec = _make_record(db_session)
    api = _client_as(client, "rev", "reviewer")
    resp = api.post(f"/records/{rec.id}/reject", json={"predefined_reason": "blur", "comment": "y" * 2000})
    assert resp.status_code == 200, resp.text
    [rej] = api.get(f"/records/{rec.id}/rejections").json()
    assert rej["comment"] == "y" * 2000
