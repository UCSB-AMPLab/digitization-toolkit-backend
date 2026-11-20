"""What is refused while an adoption's journal names a volume (app.core.adopt_journal.journal_names_volume).

A volume an unfinished adoption names has its files at one of two names
until startup recovery decides which; nothing may free, restore, move,
delete or add its files meanwhile, or recovery would find a file at
neither name. Each path that could is refused with the code
`adoption_unfinished`. The journal here is written by hand, naming the two
volumes of tests/unit/test_adopt_apply.py.

    python -m pytest tests/unit/test_adopt_journal_guards.py

Version: 0.3.3
"""

import json

import pytest

from tests.unit.test_adopt_apply import _journal, _setup
from tests.unit.test_adopt_captures_plan import SRC, _record, own_data_dir, tenant  # noqa: F401
from tests.unit.test_offsite_free import _as


@pytest.fixture
def journaled(db_session, override_projects_root, tenant):
    src, tgt, images, plan = _setup(db_session, override_projects_root)
    _journal().write_text(json.dumps({"version": 1, "run_id": "r", "source_id": src.id, "target_id": tgt.id}))
    return src, tgt, images


@pytest.mark.unit
def test_the_one_helper_answers_for_the_two_volumes_only(db_session, journaled):
    from app.core.adopt_journal import journal_names_volume
    from tests.unit.test_adopt_captures_plan import _volume

    src, tgt, _ = journaled
    other = _volume(db_session, "Otro", "xx-other-001")
    assert journal_names_volume(src.id) and journal_names_volume(tgt.id)
    assert not journal_names_volume(other.id)
    _journal().write_text("torn")
    assert journal_names_volume(other.id)


@pytest.mark.unit
def test_freeing_its_dry_run_and_restoring_are_refused(db_session, journaled, monkeypatch):
    from app.core import offsite_free

    from app.core import unit_mode

    # The unit's mode is not what is tested.
    monkeypatch.setattr(offsite_free, "_refuse_offline", lambda db: None)
    monkeypatch.setattr(unit_mode, "offsite_on", lambda db: True)

    src, tgt, _ = journaled
    for call in (offsite_free.plan_free, offsite_free._prepare_free, offsite_free._prepare_restore):
        with pytest.raises(offsite_free.FreeRefused) as refused:
            call(db_session, src.id)
        assert refused.value.detail["code"] == "adoption_unfinished"
    with pytest.raises(offsite_free.FreeRefused):
        offsite_free._free_work(db_session, src.id, SRC, "admin")
    with pytest.raises(ValueError, match="adoption"):
        offsite_free.mark_freed(db_session, SRC)


@pytest.mark.unit
def test_a_rebuild_of_the_volume_is_refused(db_session, journaled, tmp_path):
    from app.core.rebuild_volume import RebuildRefused, rebuild_volume

    with pytest.raises(RebuildRefused, match="adoption"):
        rebuild_volume(db_session, SRC, str(tmp_path))


@pytest.mark.unit
def test_the_routes_that_change_its_files_are_refused(client, db_session, journaled):
    from tests.unit.test_adopt_captures_plan import _volume

    src, tgt, images = journaled
    other = _volume(db_session, "Otro", "xx-other-001")
    rec_id, img_id = images[(2, 0)].record_id, images[(2, 0)].id
    placeholder = _record(db_session, src, "placeholder")
    _as("admin")

    responses = {
        "move-records": client.post(f"/collections/{src.id}/move-records", params={"target_collection_id": other.id}),
        "move-records into": client.post(f"/collections/{other.id}/move-records",
                                         params={"target_collection_id": tgt.id}),
        "record delete": client.delete(f"/records/{placeholder.id}"),
        "image delete": client.delete(f"/records/images/{img_id}"),
        "image upload": client.post(f"/records/{rec_id}/images", files={"file": ("x.jpg", b"jpeg", "image/jpeg")}),
        "volume delete": client.delete(f"/collections/{tgt.id}"),
        "export": client.post(f"/collections/{src.id}/export"),
        "record move": client.patch(f"/records/{placeholder.id}", json={"collection_id": other.id}),
    }

    for name, r in responses.items():
        assert r.status_code == 409, (name, r.status_code, r.text)
        assert r.json()["detail"]["code"] == "adoption_unfinished", (name, r.text)
