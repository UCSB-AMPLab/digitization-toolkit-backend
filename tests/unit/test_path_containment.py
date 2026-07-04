"""
Tests for path containment on record image endpoints.

Covers the LFI fix: stored file paths must never be served, unlinked, or
client-settable when they point outside the storage roots.
"""

import pytest


@pytest.fixture
def operator_client(client, db_session):
    """Test client authenticated as an operator-role user."""
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User
    from app.core.security import hash_password

    user = User(
        username="operator_user",
        email="operator@example.com",
        hashed_password=hash_password("testpassword"),
        role="operator",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    app.dependency_overrides[get_current_user] = lambda: user
    return client


@pytest.fixture
def secret_file(tmp_path):
    """A file outside the storage roots, standing in for /etc/passwd or .env."""
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET_KEY=super-secret-value")
    return secret


def _make_record_with_image(db_session, **image_fields):
    from app.models.record import Record, RecordImage

    rec = Record(title="Test record")
    db_session.add(rec)
    db_session.commit()
    db_session.refresh(rec)

    defaults = {"filename": "page.jpg", "file_path": "", "format": "jpg"}
    defaults.update(image_fields)
    img = RecordImage(record_id=rec.id, **defaults)
    db_session.add(img)
    db_session.commit()
    db_session.refresh(img)
    return rec, img


# ==============================================================================
# resolve_within_storage unit tests
# ==============================================================================

class TestResolveWithinStorage:
    def test_rejects_path_outside_storage(self, override_projects_root, secret_file):
        from app.core.paths import resolve_within_storage
        assert resolve_within_storage(str(secret_file)) is None

    def test_rejects_traversal_out_of_storage(self, override_projects_root, secret_file):
        from app.core.paths import resolve_within_storage
        traversal = override_projects_root / ".." / secret_file.name
        assert resolve_within_storage(str(traversal)) is None

    def test_rejects_empty_and_none(self, override_projects_root):
        from app.core.paths import resolve_within_storage
        assert resolve_within_storage(None) is None
        assert resolve_within_storage("") is None

    def test_accepts_path_inside_projects_dir(self, override_projects_root):
        from app.core.paths import resolve_within_storage
        inside = override_projects_root / "proj" / "images" / "page.jpg"
        resolved = resolve_within_storage(str(inside))
        assert resolved is not None
        assert resolved == inside.resolve()

    def test_normalizes_internal_traversal(self, override_projects_root):
        from app.core.paths import resolve_within_storage
        inside = override_projects_root / "proj" / ".." / "proj2" / "page.jpg"
        resolved = resolve_within_storage(str(inside))
        assert resolved == (override_projects_root / "proj2" / "page.jpg").resolve()


# ==============================================================================
# Endpoint tests
# ==============================================================================

class TestThumbnailPathNotClientSettable:
    def test_patch_ignores_thumbnail_path(self, operator_client, db_session, secret_file):
        rec, img = _make_record_with_image(db_session, thumbnail_path="/original/thumb.jpg")

        resp = operator_client.patch(
            f"/records/images/{img.id}",
            json={"sequence": 5, "thumbnail_path": str(secret_file)},
        )
        assert resp.status_code == 200

        db_session.refresh(img)
        assert img.sequence == 5
        assert img.thumbnail_path == "/original/thumb.jpg"

    def test_patch_ignores_file_path(self, operator_client, db_session, secret_file):
        rec, img = _make_record_with_image(db_session, file_path="/original/page.jpg")

        resp = operator_client.patch(
            f"/records/images/{img.id}",
            json={"file_path": str(secret_file)},
        )
        assert resp.status_code == 200

        db_session.refresh(img)
        assert img.file_path == "/original/page.jpg"


class TestFileServingContainment:
    def test_download_rejects_file_path_outside_storage(self, operator_client, db_session, secret_file):
        rec, img = _make_record_with_image(db_session, file_path=str(secret_file))

        resp = operator_client.get(f"/records/images/{img.id}/file")
        assert resp.status_code == 404
        assert b"super-secret-value" not in resp.content

    def test_thumbnail_rejects_poisoned_thumbnail_path(self, operator_client, db_session, secret_file):
        rec, img = _make_record_with_image(
            db_session, file_path="", thumbnail_path=str(secret_file)
        )

        resp = operator_client.get(f"/records/images/{img.id}/thumbnail")
        assert resp.status_code == 404
        assert b"super-secret-value" not in resp.content

    def test_thumbnail_rejects_traversal_path(self, operator_client, db_session, override_projects_root, secret_file):
        traversal = override_projects_root / ".." / secret_file.name
        rec, img = _make_record_with_image(
            db_session, file_path="", thumbnail_path=str(traversal)
        )

        resp = operator_client.get(f"/records/images/{img.id}/thumbnail")
        assert resp.status_code == 404
        assert b"super-secret-value" not in resp.content

    def test_download_serves_file_inside_storage(self, operator_client, db_session, override_projects_root):
        file_path = override_projects_root / "proj" / "images" / "main" / "page.jpg"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"fake-jpeg-bytes")

        rec, img = _make_record_with_image(db_session, file_path=str(file_path))

        resp = operator_client.get(f"/records/images/{img.id}/file")
        assert resp.status_code == 200
        assert resp.content == b"fake-jpeg-bytes"


class TestDeleteContainment:
    def test_delete_image_does_not_unlink_outside_storage(self, operator_client, db_session, secret_file):
        rec, img = _make_record_with_image(
            db_session, file_path=str(secret_file), thumbnail_path=str(secret_file)
        )

        resp = operator_client.delete(f"/records/images/{img.id}")
        assert resp.status_code == 200
        assert secret_file.exists()

    def test_delete_record_does_not_unlink_outside_storage(self, operator_client, db_session, secret_file):
        rec, img = _make_record_with_image(
            db_session, file_path=str(secret_file), thumbnail_path=str(secret_file)
        )

        resp = operator_client.delete(f"/records/{rec.id}")
        assert resp.status_code == 200
        assert secret_file.exists()

    def test_delete_image_unlinks_file_inside_storage(self, operator_client, db_session, override_projects_root):
        file_path = override_projects_root / "proj" / "images" / "main" / "page.jpg"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"fake-jpeg-bytes")

        rec, img = _make_record_with_image(db_session, file_path=str(file_path))

        resp = operator_client.delete(f"/records/images/{img.id}")
        assert resp.status_code == 200
        assert not file_path.exists()
