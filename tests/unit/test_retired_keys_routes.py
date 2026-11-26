"""GET /system/offsite/retired-keys: the old objects an adoption left in the bucket (NEH-387).

The list for the person who deletes them with the bucket's own
credentials: each retired key with its kind, when it was retired, the
volume and image it now belongs to, its new key and whether it is
deletable (its replacement is verified with the same SHA1), and the
totals. `?format=text` gives the deletable keys alone, one per line,
relative to the remote. Nothing here deletes anything; the unit cannot
delete in the bucket.

    python -m pytest tests/unit/test_retired_keys_routes.py

Version: 0.3.3
"""

from datetime import datetime, timezone

import pytest

from tests.unit.test_adopt_apply import _adopt, _setup
from tests.unit.test_adopt_captures_plan import TGT, own_data_dir, tenant  # noqa: F401
from tests.unit.test_offsite_free import _as

URL = "/system/offsite/retired-keys"


def _adopted(db, root):
    src, tgt, images, plan = _setup(db, root)
    _adopt(db, src, tgt)
    return src, tgt, plan


def _verify_new(db, renames):
    from app.models.offsite_object import OffsiteObject

    old = {(row.retired_image_id, row.kind): row for row in
           db.query(OffsiteObject).filter(OffsiteObject.retired_at.isnot(None))}
    for r in renames:
        for kind, key in r.new_keys.items():
            db.add(OffsiteObject(record_image_id=r.image_id, kind=kind, object_key=key,
                                 local_sha1=old[(r.image_id, kind)].local_sha1,
                                 uploaded_at=datetime.now(timezone.utc), verified_at=datetime.now(timezone.utc)))
    db.commit()


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_admin_may_list_the_retired_keys(client, db_session, override_projects_root, tenant, role):
    _adopted(db_session, override_projects_root)
    _as(role)

    assert client.get(URL).status_code == 403
    assert client.get(URL, params={"format": "text"}).status_code == 403


@pytest.mark.unit
def test_the_list_and_its_totals_before_and_after_the_replacements_verify(client, db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, plan = _adopted(db_session, root)
    _as("admin")

    body = client.get(URL).json()

    assert body["totals"] == {"keys": 14, "deletable": 0, "not_deletable": 14}
    assert "cannot delete" in body["note"]
    entry = body["keys"][0]
    assert set(entry) >= {"key", "kind", "retired_at", "volume_id", "volume_stem", "image_id", "current_key",
                          "deletable"}
    assert entry["volume_id"] == tgt.id and entry["volume_stem"] == TGT and entry["deletable"] is False
    assert client.get(URL, params={"format": "text"}).text == ""

    _verify_new(db_session, plan.renames[:2])
    body = client.get(URL).json()
    assert body["totals"] == {"keys": 14, "deletable": 4, "not_deletable": 10}
    deletable = sorted(k["key"] for k in body["keys"] if k["deletable"])
    assert all(k["current_key"].startswith(f"captua-test/{TGT}/") for k in body["keys"] if k["deletable"])

    r = client.get(URL, params={"format": "text"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert sorted(r.text.splitlines()) == deletable and r.text.endswith("\n")


@pytest.mark.unit
def test_listing_the_keys_deletes_nothing(client, db_session, override_projects_root, tenant):
    from app.models.offsite_object import OffsiteObject

    root = override_projects_root
    _adopted(db_session, root)
    before = sorted(r.object_key for r in db_session.query(OffsiteObject))
    _as("admin")

    client.get(URL)
    client.get(URL, params={"format": "text"})

    db_session.expire_all()
    assert sorted(r.object_key for r in db_session.query(OffsiteObject)) == before
