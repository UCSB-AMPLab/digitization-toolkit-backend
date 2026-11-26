"""Deleting the volume an adoption emptied (NEH-387).

A volume whose captures were adopted away may be deleted
(Collection.deletable_after_adoption): it holds no record, no image and no
sub-volume, no adoption journal names it, and every offsite key under its
stem is a retired row. Its stem stays held by those keys, so no volume can
take it. Its manifest is moved to `.retired-manifests/`
(storage_ops.retire_project_manifest), where the fixity index keeps
reading it, and its emptied directories are removed. Every other volume
that has captured is refused as before.

    python -m pytest tests/unit/test_adopt_source_delete.py

Version: 0.3.3
"""

import hashlib
from pathlib import Path

import pytest

from tests.unit.test_adopt_apply import _adopt, _journal, _setup
from tests.unit.test_adopt_captures_plan import SRC, TGT, _record, _volume, own_data_dir, tenant  # noqa: F401
from tests.unit.test_offsite_free import _as


def _adopted(db, root):
    src, tgt, _, plan = _setup(db, root)
    _adopt(db, src, tgt)
    return src, tgt, plan


@pytest.mark.unit
def test_the_emptied_source_can_be_deleted_and_its_manifest_is_kept(client, db_session, override_projects_root, tenant):
    from app.core.integrity import RETIRED_MANIFESTS_DIR, build_fixity_index
    from app.models.collection import Collection

    root = override_projects_root
    src, tgt, plan = _adopted(db_session, root)
    src_id, manifest = src.id, (root / SRC / "metadata" / "manifest.jsonl").read_bytes()
    _as("admin")

    r = client.delete(f"/collections/{src_id}")

    assert r.status_code == 204, r.text
    db_session.expire_all()
    assert db_session.get(Collection, src_id) is None
    assert not (root / SRC).exists()
    [retired] = list((root / RETIRED_MANIFESTS_DIR).glob(f"{SRC}-*-manifest.jsonl"))
    assert retired.read_bytes() == manifest
    index = build_fixity_index(root.resolve())
    for r_ in plan.renames:
        sha = hashlib.sha256(r_.new_path.read_bytes()).hexdigest()
        assert index.expected(r_.capture_id, r_.image_id, r_.new_path.resolve()) == sha
    # And without the source's manifest at all: the target holds its own copies.
    retired.unlink()
    index = build_fixity_index(root.resolve())
    for r_ in plan.renames:
        sha = hashlib.sha256(r_.new_path.read_bytes()).hexdigest()
        assert index.expected(r_.capture_id, None, r_.new_path.resolve()) == sha


@pytest.mark.unit
def test_a_file_left_in_the_source_directory_stays_and_the_delete_goes_on(client, db_session, override_projects_root, tenant, caplog):
    import logging

    root = override_projects_root
    src, tgt, _ = _adopted(db_session, root)
    note = root / SRC / "notes.txt"
    note.write_text("by hand")
    _as("admin")

    with caplog.at_level(logging.WARNING):
        r = client.delete(f"/collections/{src.id}")

    assert r.status_code == 204, r.text
    assert note.is_file() and any(str(note) in rec.getMessage() for rec in caplog.records)


@pytest.mark.unit
def test_the_deleted_sources_stem_stays_held_by_its_retired_keys(client, db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _ = _adopted(db_session, root)
    _as("admin")
    assert client.delete(f"/collections/{src.id}").status_code == 204

    r = client.post("/collections/", json={"name": "Again", "filename_stem": SRC,
                                           "project_id": tgt.project_id})

    assert r.status_code == 409, r.text


@pytest.mark.unit
def test_a_source_an_adoption_journal_names_is_not_deleted(client, db_session, override_projects_root, tenant):
    import json

    root = override_projects_root
    src, tgt, _ = _adopted(db_session, root)
    _journal().write_text(json.dumps({"version": 1, "run_id": "r", "source_id": src.id, "target_id": tgt.id}))
    _as("admin")

    assert client.delete(f"/collections/{src.id}").status_code == 409


@pytest.mark.unit
def test_a_volume_with_a_raised_counter_and_no_adoption_is_still_refused(client, db_session, override_projects_root, tenant):
    from app.models.collection import STEM_RESERVED_MESSAGE

    vol = _volume(db_session, "Hand emptied", "xx-hand-001", capture_counter=4)
    _as("admin")

    r = client.delete(f"/collections/{vol.id}")

    assert (r.status_code, r.json()["detail"]) == (409, {"message": STEM_RESERVED_MESSAGE})


@pytest.mark.unit
def test_a_source_with_one_key_that_is_not_retired_is_still_refused(client, db_session, override_projects_root, tenant):
    from app.models.collection import STEM_RESERVED_MESSAGE
    from app.models.offsite_object import OffsiteObject

    root = override_projects_root
    src, tgt, _ = _adopted(db_session, root)
    db_session.add(OffsiteObject(record_image_id=None, kind="capture", local_sha1="9" * 40,
                                 object_key=f"captua-test/{SRC}/captures/{SRC}-img_0099_c0.CR2"))
    db_session.commit()
    _as("admin")

    r = client.delete(f"/collections/{src.id}")

    assert (r.status_code, r.json()["detail"]) == (409, {"message": STEM_RESERVED_MESSAGE})


@pytest.mark.unit
def test_the_orm_guard_follows_the_same_rule(db_session, override_projects_root, tenant):
    from app.models.collection import Collection, StemReservedError

    root = override_projects_root
    src, tgt, _ = _adopted(db_session, root)
    hand = _volume(db_session, "Hand emptied", "xx-hand-001", capture_counter=4)

    db_session.delete(db_session.get(Collection, hand.id))
    with pytest.raises(StemReservedError):
        db_session.flush()
    db_session.rollback()
    db_session.delete(db_session.get(Collection, src.id))
    db_session.commit()
    assert db_session.get(Collection, src.id) is None
