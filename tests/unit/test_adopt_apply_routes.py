"""The admin routes of an adoption: GET is the plan, POST applies it.

GET /collections/{source_id}/adopt-into/{target_id} returns the plan
(app.core.adopt_captures.plan_adopt_captures) and changes nothing; POST
applies it (app.core.adopt_apply.adopt_captures) and returns its summary,
409 with the refusal codes and details, or 503 when the bucket listing
cannot answer. Both are for an admin only. The fixtures are those of
tests/unit/test_adopt_apply.py; the bucket listing is replaced by an empty
one, or one that cannot answer, through the module's listing function.

    python -m pytest tests/unit/test_adopt_apply_routes.py

Version: 0.3.3
"""

import pytest

from tests.unit.test_adopt_apply import _setup, _snapshot
from tests.unit.test_adopt_captures_plan import TGT, _record, own_data_dir, tenant  # noqa: F401
from tests.unit.test_offsite_free import _as


@pytest.fixture
def empty_bucket(monkeypatch):
    from app.core import adopt_apply

    monkeypatch.setattr(adopt_apply, "bucket_keys_under_stem", lambda tenant, stem: [])


def _url(src, tgt):
    return f"/collections/{src.id}/adopt-into/{tgt.id}"


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_admin_may_plan_or_apply_an_adoption(client, db_session, override_projects_root, tenant, empty_bucket, role):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)
    _as(role)

    assert client.get(_url(src, tgt)).status_code == 403
    assert client.post(_url(src, tgt)).status_code == 403
    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_the_dry_run_is_the_plan_and_changes_nothing(client, db_session, override_projects_root, tenant, empty_bucket):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    before = _snapshot(db_session, root)
    _as("admin")

    r = client.get(_url(src, tgt), params={"limit": 3})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["may_adopt"] is True and body["refusals"] == [] and body["details"] == {}
    assert (body["source_stem"], body["target_stem"]) == (plan.source_stem, plan.target_stem)
    assert (body["source_project_id"], body["target_project_id"]) == (plan.source_project_id, plan.target_project_id)
    assert body["project_changes"] is True
    assert (body["records"], body["images_current"], body["images_superseded"]) == (4, 6, 2)
    assert body["bytes"] == plan.bytes
    assert (body["counter_before"], body["counter_after"]) == (0, 5)
    assert body["renames_count"] == 8 and body["renames_truncated"] is True and len(body["renames"]) == 3
    first = plan.renames[0]
    assert body["renames"][0] == {
        "image_id": first.image_id, "record_id": first.record_id, "is_current": first.is_current,
        "number": first.number, "old_name": first.old_name, "new_name": first.new_name,
        "old_path": str(first.old_path), "new_path": str(first.new_path),
        "old_preview_path": str(first.old_preview_path), "new_preview_path": str(first.new_preview_path),
        "old_raw_buffer_path": None, "new_raw_buffer_path": None,
        "old_thumbnail_path": str(first.old_thumbnail_path), "new_thumbnail_path": str(first.new_thumbnail_path),
        "old_pair_id": first.old_pair_id, "new_pair_id": first.new_pair_id, "capture_id": first.capture_id,
        "old_keys": first.old_keys, "new_keys": first.new_keys,
    }
    assert body["uncovered"] == [] and body["thumbnails_missing"] == []
    assert client.get(_url(src, tgt)).json()["renames_truncated"] is False
    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_the_dry_run_of_a_refused_adoption_lists_its_refusals(client, db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _record(db_session, tgt, "already here")
    _as("admin")

    r = client.get(_url(src, tgt))

    assert r.status_code == 200, r.text
    assert r.json()["may_adopt"] is False and r.json()["refusals"] == ["target_has_records"]
    assert client.get(f"/collections/{src.id}/adopt-into/999999").json()["refusals"] == ["target_not_found"]


@pytest.mark.unit
def test_post_applies_the_adoption(client, db_session, override_projects_root, tenant, empty_bucket):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _as("admin")

    r = client.post(_url(src, tgt))

    assert r.status_code == 200, r.text
    assert r.json() == {"source_id": src.id, "target_id": tgt.id, "records": 4, "images": 8,
                        "files_renamed": 23, "rows_retired": 14, "counter_after": 5,
                        "colour_card_sets": 0, "colour_card_shots": 0,
                        "source_lines_appended": 8, "target_lines_appended": True,
                        "target_lines_pending": False, "recovery_pending": False}
    assert all(r_.new_path.is_file() and not r_.old_path.exists() for r_ in plan.renames)


@pytest.mark.unit
def test_post_of_a_refused_adoption_is_409_with_the_codes(client, db_session, override_projects_root, tenant, empty_bucket):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _record(db_session, tgt, "already here")
    before = _snapshot(db_session, root)
    _as("admin")

    r = client.post(_url(src, tgt))

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["codes"] == ["target_has_records"] and detail["details"] == {}
    assert detail["message"]
    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_post_when_the_listing_cannot_answer_is_503(client, db_session, override_projects_root, tenant, monkeypatch):
    from app.core import adopt_apply
    from app.core.catalogue_backup import VerificationUnavailable

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)

    def unavailable(tenant, stem):
        raise VerificationUnavailable("401 unauthorized")

    monkeypatch.setattr(adopt_apply, "bucket_keys_under_stem", unavailable)
    _as("admin")

    r = client.post(_url(src, tgt))

    assert r.status_code == 503, r.text
    assert r.json()["detail"]["codes"] == ["listing_unavailable"]
    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_post_while_a_volume_is_held_is_409(client, db_session, override_projects_root, tenant, empty_bucket):
    from app.core.export_jobs import export_jobs

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _as("admin")

    with export_jobs.hold_for_reading([src.id]):
        r = client.post(_url(src, tgt))

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["codes"] == ["volume_held"]


@pytest.mark.unit
def test_both_routes_refuse_an_unauthenticated_request(client, db_session, override_projects_root, tenant, empty_bucket):
    from app.api.auth import get_current_user
    from app.main import app

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)
    app.dependency_overrides.pop(get_current_user, None)

    assert client.get(_url(src, tgt)).status_code == 401
    assert client.post(_url(src, tgt)).status_code == 401
    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_an_adoption_naming_a_testing_volume_is_not_found_and_refused_below(
        client, db_session, override_projects_root, tenant, empty_bucket):
    """With a real session the space rule answers 404; the plan, and so the apply, refuses besides."""
    from app.core.adopt_apply import AdoptRefused
    from app.core.adopt_captures import plan_adopt_captures
    from app.core.security import create_access_token
    from app.models.user import User
    from tests.unit.test_adopt_apply import _adopt
    from tests.unit.test_adopt_captures_plan import _testing_volume

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    testing = _testing_volume(db_session, "000903")
    admin = User(username="adopt-admin", hashed_password="x", role="admin", is_active=True)
    db_session.add(admin)
    db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token(str(admin.id))}"}
    before = _snapshot(db_session, root)

    for url in (_url(testing, tgt), _url(src, testing)):
        for method in ("GET", "POST"):
            r = client.request(method, url, headers=headers)
            assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found", (method, url, r.text)
    assert _snapshot(db_session, root) == before

    assert plan_adopt_captures(db_session, testing.id, tgt.id).refusals == ["source_testing_volume"]
    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, testing)
    assert refused.value.codes == ["target_testing_volume"]
    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, testing, tgt)
    assert refused.value.codes == ["source_testing_volume"]
    assert _snapshot(db_session, root) == before
