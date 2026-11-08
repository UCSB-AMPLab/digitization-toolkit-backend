"""Volumes Captua keeps on the unit (NEH-320): collections.kept_on_unit.

A volume made in Scanner only (offline) mode is kept on the unit: none of
its images is copied offsite and none of its Fisqua manifests is promoted
or sent until an admin releases it, one at a time or all of them when
switching out of offline mode. An admin may also keep a volume on the unit
in any mode, at creation or later while nothing of it has left the unit;
a kept volume is not checked with Fisqua until it is released. A handover
always queues its manifest, whatever the configuration.

    python -m pytest tests/unit/test_kept_on_unit.py

Version: 0.3.3
"""

import importlib.util
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from tests.unit.test_capture_manifest_export import TENANT, _as, tenant, vol  # noqa: F401  (fixtures)
from tests.unit.test_fisqua_outbox import (  # noqa: F401  (fisqua is a fixture)
    FakeTransport, _delivered, _handover, _rows, _run, fisqua,
)
from tests.unit.test_list_import import (  # noqa: F401  (reports_root is a fixture)
    _as as _as_importer, _item, _list_with, reports_root,
)
from tests.unit.test_unit_mode import (  # noqa: F401  (link and recipient are fixtures)
    _found, _logs, _project, _result, _unit, link, recipient,
)

REVISION = "b6d8f0a2c4e7"
MIGRATION_FILE = f"{REVISION}_add_collection_kept_on_unit.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kept(db, collection_id):
    from app.models.collection import Collection
    db.expire_all()
    return db.get(Collection, collection_id).kept_on_unit


def _set_kept(db, col, value=True):
    col.kept_on_unit = value
    db.commit()
    db.refresh(col)


def _create(client, role, project, stem="xx-test-500", **extra):
    return _as(client, role).post("/collections/", json={
        "name": "T", "project_id": project.id, "filename_stem": stem, **extra})


def _import(client, payload, kept=None):
    import json
    body = json.dumps(payload).encode("utf-8")
    data = {} if kept is None else {"kept_on_unit": "true" if kept else "false"}
    return client.post("/imports/digitisation-list", data=data,
                       files={"file": ("list.json", body, "application/json")})


def _detail(resp, status, code):
    assert resp.status_code == status, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == code, detail
    return detail


def _problem_codes(resp):
    return [p["code"] for p in resp.json()["detail"]["problems"]]


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def _load_migration(backend_root):
    path = backend_root / "alembic" / "versions" / MIGRATION_FILE
    spec = importlib.util.spec_from_file_location("kept_on_unit_migration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_migration(engine, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


@pytest.mark.unit
def test_the_migration_follows_unit_settings_and_is_the_only_head(backend_root):
    import re
    mod = _load_migration(backend_root)
    assert mod.revision == REVISION and mod.down_revision == "d7f9a1c3e5b2"
    revisions, downs = set(), set()
    for path in (backend_root / "alembic" / "versions").glob("*.py"):
        text = path.read_text()
        revisions.add(re.search(r"^revision(?:: str)? = '(\w+)'", text, re.M).group(1))
        downs.update(re.findall(r"'(\w+)'", re.search(r"^down_revision[^=]*= (.*)$", text, re.M).group(1)))
    assert revisions - downs == {REVISION}
    assert "marks no existing volume" in mod.__doc__


@pytest.mark.unit
def test_the_migration_adds_the_flag_false_on_every_volume_and_drops_it(backend_root, tmp_path):
    mod = _load_migration(backend_root)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    md = sa.MetaData()
    sa.Table("collections", md, sa.Column("id", sa.Integer, primary_key=True),
             sa.Column("name", sa.String(255), nullable=False))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO collections (id, name) VALUES (1, 'a'), (2, 'b')"))

    _run_migration(engine, mod.upgrade)
    column = {c["name"]: c for c in sa.inspect(engine).get_columns("collections")}["kept_on_unit"]
    assert column["nullable"] is False
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT kept_on_unit FROM collections ORDER BY id")).scalars().all() == [0, 0]
        conn.execute(sa.text("INSERT INTO collections (id, name) VALUES (3, 'c')"))
        assert conn.execute(sa.text("SELECT kept_on_unit FROM collections WHERE id = 3")).scalar() == 0
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("UPDATE collections SET kept_on_unit = NULL WHERE id = 1"))

    _run_migration(engine, mod.downgrade)
    assert "kept_on_unit" not in {c["name"] for c in sa.inspect(engine).get_columns("collections")}


@pytest.mark.unit
def test_the_model_matches_the_migration():
    from app.models.collection import Collection
    col = Collection.__table__.c.kept_on_unit
    assert col.nullable is False and col.server_default is not None


# ---------------------------------------------------------------------------
# R1, R9: creation
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode, kept", [("offline", True), ("backup", False)])
def test_post_collections_keeps_a_volume_made_in_offline_mode_only(client, db_session, mode, kept):
    _unit(db_session, mode, projects_enabled=True)
    resp = _create(client, "operator", _project(db_session, "P"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["kept_on_unit"] is kept
    assert _kept(db_session, resp.json()["id"]) is kept


@pytest.mark.unit
def test_post_collections_in_fisqua_mode_does_not_keep_a_volume(client, db_session, link):
    _unit(db_session, "fisqua")
    link.description_answers = [_found("xx-test-500")]
    resp = _create(client, "admin", _project(db_session, "P"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["kept_on_unit"] is False
    assert link.descriptions_asked == ["xx-test-500"]


@pytest.mark.unit
@pytest.mark.parametrize("mode, kept", [("offline", True), ("backup", False)])
def test_the_list_import_keeps_volumes_made_in_offline_mode_only(client, db_session, reports_root, mode, kept):
    from app.models.collection import Collection
    _unit(db_session, mode)
    _as_importer("op", "operator")
    resp = _import(client, _list_with([_item()]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["keptOnUnit"] is kept
    db_session.expire_all()
    assert db_session.query(Collection).filter_by(filename_stem="xx-test-901").one().kept_on_unit is kept


@pytest.mark.unit
def test_the_list_import_in_fisqua_mode_does_not_keep_volumes(client, db_session, link, reports_root):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    _as_importer("boss", "admin")
    resp = _import(client, _list_with([_item()]))
    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.query(Collection).filter_by(filename_stem="xx-test-901").one().kept_on_unit is False


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["backup", "fisqua"])
def test_an_admin_may_keep_a_new_volume_in_any_mode(client, db_session, link, mode):
    _unit(db_session, mode)
    resp = _create(client, "admin", _project(db_session, "P"), kept_on_unit=True)
    assert resp.status_code == 201, resp.text
    assert _kept(db_session, resp.json()["id"]) is True


@pytest.mark.unit
def test_an_operator_cannot_keep_a_new_volume_outside_offline_mode(client, db_session):
    from app.models.collection import Collection
    _unit(db_session, "backup")
    resp = _create(client, "operator", _project(db_session, "P"), kept_on_unit=True)
    _detail(resp, 403, "kept_on_unit_admin_only")
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
def test_in_offline_mode_a_false_is_ignored_and_an_operators_true_is_redundant(client, db_session):
    _unit(db_session, "offline", projects_enabled=True)
    p = _project(db_session, "P")
    resp = _create(client, "admin", p, kept_on_unit=False)
    assert resp.status_code == 201 and resp.json()["kept_on_unit"] is True
    resp = _create(client, "operator", p, stem="xx-test-501", kept_on_unit=True)
    assert resp.status_code == 201 and resp.json()["kept_on_unit"] is True


@pytest.mark.unit
def test_an_admin_list_import_may_keep_its_volumes_and_an_operators_is_refused(
        client, db_session, reports_root):
    from app.models.collection import Collection
    _unit(db_session, "backup")
    _as_importer("op", "operator")
    _detail(_import(client, _list_with([_item()]), kept=True), 403, "kept_on_unit_admin_only")
    assert db_session.query(Collection).count() == 0
    _as_importer("boss", "admin")
    resp = _import(client, _list_with([_item()]), kept=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["keptOnUnit"] is True
    db_session.expire_all()
    assert db_session.query(Collection).one().kept_on_unit is True


@pytest.mark.unit
def test_an_admin_list_import_kept_in_fisqua_mode_says_so_in_the_override(client, db_session, link, reports_root):
    _unit(db_session, "fisqua")
    _as_importer("boss", "admin")
    resp = _import(client, _list_with([_item()]), kept=True)
    assert resp.status_code == 200, resp.text
    [log] = _logs(db_session, "list_import_override")
    assert "kept on the unit" in log.detail


# ---------------------------------------------------------------------------
# R10: a kept volume is not checked with Fisqua
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_admin_kept_volume_in_fisqua_mode_is_created_without_a_lookup_and_logged(client, db_session, link):
    _unit(db_session, "fisqua")
    link.description_answers = [_result("unknown_code", status_code=404)]
    resp = _create(client, "admin", _project(db_session, "P"), stem="xx-test-test", kept_on_unit=True)
    assert resp.status_code == 201, resp.text
    assert link.descriptions_asked == []
    [log] = _logs(db_session, "volume_created_override")
    assert "xx-test-test" in log.detail and "kept on the unit" in log.detail


@pytest.mark.unit
def test_an_admin_volume_not_kept_in_fisqua_mode_is_still_checked(client, db_session, link):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    link.description_answers = [_result("unknown_code", status_code=404)]
    resp = _create(client, "admin", _project(db_session, "P"), stem="xx-test-test", kept_on_unit=False)
    _detail(resp, 422, "unknown_reference_code")
    assert link.descriptions_asked == ["xx-test-test"]
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
def test_an_admin_edits_a_kept_volumes_stem_in_fisqua_mode_without_a_lookup(client, db_session, link):
    from app.models.collection import Collection
    _unit(db_session, "fisqua")
    p = _project(db_session, "P")
    col = Collection(name="T", project_id=p.id, filename_stem="xx-test-600", kept_on_unit=True)
    db_session.add(col)
    db_session.commit()
    resp = _as(client, "admin").patch(f"/collections/{col.id}", json={"filename_stem": "xx-test-601"})
    assert resp.status_code == 200, resp.text
    assert link.descriptions_asked == []
    assert len(_logs(db_session, "volume_code_override")) == 1
    # Operators stay refused, and the stem stays required.
    _detail(_as(client, "operator").patch(f"/collections/{col.id}", json={"filename_stem": "xx-test-602"}),
            403, "volumes_from_fisqua")
    _detail(_as(client, "admin").patch(f"/collections/{col.id}", json={"filename_stem": None}),
            422, "stem_required")


# ---------------------------------------------------------------------------
# R2: what the flag stops
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_upload_work_leaves_a_kept_volume_out_until_it_is_released(db_session, vol):
    from app.core.image_upload import pending_work
    vol.record(upload=False)
    _set_kept(db_session, vol.col)
    assert pending_work(db_session) == []
    _set_kept(db_session, vol.col, False)
    assert {w.stem for w in pending_work(db_session)} == {vol.stem}


@pytest.mark.unit
def test_a_keep_committed_after_the_work_list_is_built_stops_the_object(db_session, vol):
    from app.core import image_upload
    vol.record(upload=False)
    [item, *_] = image_upload.pending_work(db_session)
    _set_kept(db_session, vol.col)
    result = image_upload.RunResult()
    assert image_upload._read_object(db_session, item, result) is None
    assert result.skipped[0]["reason"] == "volume kept on the unit"


@pytest.mark.unit
def test_a_kept_volumes_manifest_waits_and_is_sent_after_release(client, db_session, vol, link):
    vol.record(status="captured")  # its objects verified, as if they had been copied
    _handover(client, vol.col)
    _set_kept(db_session, vol.col)
    transport = FakeTransport(sends=[_delivered()])
    _run(db_session, transport)
    assert transport.sent == []
    assert [r.state for r in _rows(db_session, vol.col)] == ["waiting_for_upload"]

    link.description_answers = [_found(vol.stem)]  # fisqua mode: the release checks the code
    resp = _as(client, "admin").post(f"/collections/{vol.col.id}/release-from-unit")
    assert resp.status_code == 200, resp.text
    _run(db_session, transport)
    assert len(transport.sent) == 1
    assert [r.state for r in _rows(db_session, vol.col)] == ["delivered"]


@pytest.mark.unit
def test_a_kept_volume_made_in_fisqua_mode_is_neither_uploaded_nor_sent(client, db_session, link,
                                                                      override_projects_root):
    from app.core.image_upload import pending_work
    from tests.unit.test_capture_manifest_export import Volume
    _unit(db_session, "fisqua")
    resp = _create(client, "admin", _project(db_session, "P"), stem="xx-test-700", kept_on_unit=True)
    assert resp.status_code == 201, resp.text
    from app.models.collection import Collection
    col = db_session.get(Collection, resp.json()["id"])
    v = Volume.__new__(Volume)  # the helper's record and capture methods, on the volume just created
    v.db, v.tenant, v.stem, v.col, v.ordinal = db_session, TENANT, col.filename_stem, col, 0
    v.root = override_projects_root / v.stem
    for sub in ("captures", "previews", "metadata"):
        (v.root / sub).mkdir(parents=True, exist_ok=True)
    # Its objects verified, so the volume reads as received: only the kept
    # guard in the outbox's promotion stands between it and Fisqua.
    v.record(status="captured", upload=True)
    _handover(client, col)
    transport = FakeTransport(sends=[_delivered()])
    _run(db_session, transport)
    assert transport.sent == []
    assert [r.state for r in _rows(db_session, col)] == ["waiting_for_upload"]
    # And a capture not yet copied is not upload work.
    v.capture(col.records[0], ("left",), upload=False)
    assert pending_work(db_session) == []


@pytest.mark.unit
def test_a_kept_volume_cannot_be_freed_or_marked_freed(db_session, vol, fisqua):
    from app.core import offsite_free
    vol.record()  # every object verified
    _set_kept(db_session, vol.col)
    with pytest.raises(offsite_free.FreeRefused) as refused:
        offsite_free.plan_free(db_session, vol.col.id)
    assert refused.value.detail["code"] == "kept_on_unit"
    with pytest.raises(offsite_free.FreeRefused):
        offsite_free.start_free(db_session, vol.col.id, "boss")
    with pytest.raises(ValueError, match="keeps"):
        offsite_free.mark_freed(db_session, vol.stem)


# ---------------------------------------------------------------------------
# R3: a handover always queues
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_handover_on_an_unconfigured_unit_queues_its_manifest_and_sends_nothing(client, db_session, vol):
    vol.record(status="captured")
    _handover(client, vol.col)
    assert [r.state for r in _rows(db_session, vol.col)] == ["waiting_for_upload"]
    transport = FakeTransport(sends=[_delivered()])
    assert _run(db_session, transport)["status"] == "disabled"
    assert transport.sent == []
    # The volume's answer, the poll and the unit summary still say nothing of it.
    from app.core.fisqua_outbox import unit_fisqua_summary, volume_fisqua_state, volumes_to_poll
    assert volume_fisqua_state(db_session, vol.col.id) is None
    assert volumes_to_poll(db_session) == {}
    assert unit_fisqua_summary(db_session)["configured"] is False


# ---------------------------------------------------------------------------
# R4: counts
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_kept_volume_counts_as_pending_nowhere(client, db_session, vol, override_projects_root):
    from app.core.image_upload import unit_upload_summary
    from tests.unit.test_capture_manifest_export import Volume
    vol.record(upload=False)                       # 2 files
    other = Volume(db_session, override_projects_root, stem="xx-test-800", external_id="xx-test-800",
                   name="Other")
    other.record(upload=False)                     # 2 files, not kept
    _set_kept(db_session, vol.col)

    state = _as(client, "reviewer").get(f"/collections/{vol.col.id}/offsite").json()
    assert state["kept_on_unit"] is True and state["pending"] == 0 and state["total"] == 2
    assert _as(client, "reviewer").get(f"/collections/{other.col.id}/offsite").json()["kept_on_unit"] is False

    summary = unit_upload_summary(db_session)
    assert summary["kept_volumes"] == 1 and summary["kept_files"] == 2
    assert summary["volumes"] == 1 and summary["total"] == 2 and summary["pending"] == 2


# ---------------------------------------------------------------------------
# R5: the switch out of offline mode
# ---------------------------------------------------------------------------

def _kept_volume(db, name="Kept", stem="xx-test-900"):
    from app.models.collection import Collection
    p = _project(db, f"P {name}")
    col = Collection(name=name, project_id=p.id, filename_stem=stem, kept_on_unit=True)
    db.add(col)
    db.commit()
    db.refresh(col)
    return col


@pytest.mark.unit
def test_get_unit_lists_the_kept_volumes_with_their_files(client, db_session, vol):
    vol.record(upload=False)
    _set_kept(db_session, vol.col)
    body = _as(client, "reviewer").get("/system/unit").json()
    assert body["keptVolumes"] == [{"id": vol.col.id, "name": vol.col.name, "files": 2}]


@pytest.mark.unit
def test_leaving_offline_with_kept_volumes_and_no_decision_is_refused(client, db_session, link):
    from app.core import unit_mode
    _unit(db_session, "offline")
    col = _kept_volume(db_session)
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup"})
    assert resp.status_code == 409, resp.text
    assert _problem_codes(resp) == ["kept_volumes_decision"]
    assert resp.json()["detail"]["problems"][0]["message"]
    assert unit_mode.current(db_session).mode == "offline"
    assert _kept(db_session, col.id) is True
    assert _logs(db_session, "unit_mode_changed") == []


@pytest.mark.unit
def test_leaving_offline_including_the_kept_volumes_releases_them_with_the_switch(client, db_session, link):
    from app.core import unit_mode
    _unit(db_session, "offline")
    a, b = _kept_volume(db_session, "A", "xx-test-901"), _kept_volume(db_session, "B", "xx-test-902")
    resp = _as(client, "admin", "boss").put("/system/unit", json={"mode": "backup", "keptVolumes": "include"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["keptVolumes"] == []
    assert unit_mode.current(db_session).mode == "backup"
    assert _kept(db_session, a.id) is False and _kept(db_session, b.id) is False
    [log] = _logs(db_session, "kept_volumes_released")
    assert log.actor == "boss" and "2" in log.detail and f"{a.id}, {b.id}" in log.detail


@pytest.mark.unit
def test_leaving_offline_keeping_the_kept_volumes_changes_no_volume(client, db_session, link):
    from app.core import unit_mode
    _unit(db_session, "offline")
    col = _kept_volume(db_session)
    resp = _as(client, "admin", "boss").put("/system/unit", json={"mode": "backup", "keptVolumes": "keep"})
    assert resp.status_code == 200, resp.text
    assert unit_mode.current(db_session).mode == "backup"
    assert _kept(db_session, col.id) is True
    assert [v["id"] for v in resp.json()["keptVolumes"]] == [col.id]
    [log] = _logs(db_session, "kept_volumes_kept")
    assert log.actor == "boss" and "1" in log.detail
    assert _logs(db_session, "kept_volumes_released") == []


@pytest.mark.unit
@pytest.mark.parametrize("decision", [None, "include", "keep"])
def test_into_offline_the_decision_is_ignored(client, db_session, link, decision):
    _unit(db_session, "backup")
    col = _kept_volume(db_session)
    body = {"mode": "offline"} if decision is None else {"mode": "offline", "keptVolumes": decision}
    resp = _as(client, "admin").put("/system/unit", json=body)
    assert resp.status_code == 200, resp.text
    assert _kept(db_session, col.id) is True
    assert _logs(db_session, "kept_volumes_released") == [] and _logs(db_session, "kept_volumes_kept") == []


@pytest.mark.unit
def test_leaving_offline_with_no_kept_volume_needs_no_decision(client, db_session, link):
    _unit(db_session, "offline")
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup"})
    assert resp.status_code == 200, resp.text
    resp = _as(client, "admin").put("/system/unit", json={"mode": "offline"})
    assert resp.status_code == 200
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup", "keptVolumes": "include"})
    assert resp.status_code == 200, resp.text
    assert _logs(db_session, "kept_volumes_released") == []


@pytest.mark.unit
def test_a_switch_refused_for_another_reason_leaves_the_flags(client, db_session, link):
    from app.core import unit_mode
    _unit(db_session, "offline")
    col = _kept_volume(db_session)
    link.operators_answers = [_result("error", error="HTTP 503")]
    resp = _as(client, "admin").put("/system/unit", json={"mode": "fisqua", "keptVolumes": "include"})
    assert resp.status_code == 409, resp.text
    assert _problem_codes(resp) == ["fisqua_unreachable"]
    assert unit_mode.current(db_session).mode == "offline"
    assert _kept(db_session, col.id) is True
    assert _logs(db_session, "kept_volumes_released") == []


@pytest.mark.unit
def test_missing_prerequisites_and_the_decision_are_refused_together(client, db_session):
    _unit(db_session, "offline")
    col = _kept_volume(db_session)
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup"})
    assert resp.status_code == 409
    assert "kept_volumes_decision" in _problem_codes(resp)
    assert "offsite_not_configured" in _problem_codes(resp)
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup", "keptVolumes": "include"})
    assert resp.status_code == 409 and "kept_volumes_decision" not in _problem_codes(resp)
    assert _kept(db_session, col.id) is True


@pytest.mark.unit
def test_an_unknown_decision_is_a_422(client, db_session):
    _unit(db_session, "offline")
    assert _as(client, "admin").put("/system/unit", json={"mode": "backup", "keptVolumes": "maybe"}).status_code == 422


# ---------------------------------------------------------------------------
# R6, R10: release one volume
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["offline", "backup"])
def test_an_admin_releases_a_kept_volume_without_a_call(client, db_session, link, mode):
    _unit(db_session, mode)
    col = _kept_volume(db_session)
    resp = _as(client, "admin", "boss").post(f"/collections/{col.id}/release-from-unit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["collection_id"] == col.id and resp.json()["kept_on_unit"] is False
    assert _kept(db_session, col.id) is False
    assert link.descriptions_asked == []
    [log] = _logs(db_session, "volume_released_from_unit")
    assert log.actor == "boss" and log.subject == col.name


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_admin_releases_a_volume(client, db_session, role):
    col = _kept_volume(db_session)
    assert _as(client, role).post(f"/collections/{col.id}/release-from-unit").status_code == 403
    assert _kept(db_session, col.id) is True


@pytest.mark.unit
def test_releasing_a_volume_not_kept_is_a_409(client, db_session):
    _unit(db_session, "backup")
    col = _kept_volume(db_session)
    _set_kept(db_session, col, False)
    _detail(_as(client, "admin").post(f"/collections/{col.id}/release-from-unit"), 409, "not_kept_on_unit")
    assert _as(client, "admin").post("/collections/99999/release-from-unit").status_code == 404


@pytest.mark.unit
def test_release_in_fisqua_mode_is_refused_on_a_code_fisqua_does_not_know(client, db_session, link):
    _unit(db_session, "fisqua")
    col = _kept_volume(db_session, stem="xx-test-test")
    link.description_answers = [_result("unknown_code", status_code=404)]
    resp = _as(client, "admin").post(f"/collections/{col.id}/release-from-unit")
    _detail(resp, 422, "unknown_reference_code")
    assert link.descriptions_asked == ["xx-test-test"]
    assert _kept(db_session, col.id) is True
    assert _logs(db_session, "volume_released_from_unit") == []


@pytest.mark.unit
def test_release_in_fisqua_mode_goes_ahead_on_a_known_code(client, db_session, link):
    _unit(db_session, "fisqua")
    col = _kept_volume(db_session, stem="xx-test-910")
    link.description_answers = [_found("xx-test-910")]
    resp = _as(client, "admin").post(f"/collections/{col.id}/release-from-unit")
    assert resp.status_code == 200, resp.text
    assert link.descriptions_asked == ["xx-test-910"]
    assert _kept(db_session, col.id) is False


# ---------------------------------------------------------------------------
# R9: keep an existing volume
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["offline", "backup", "fisqua"])
def test_an_admin_keeps_a_volume_nothing_of_which_left_the_unit(client, db_session, vol, link, mode):
    _unit(db_session, mode)
    vol.record(status="captured", upload=False)
    vol.offsite(vol.col.records[0].images[0], "capture", f"{TENANT}/{vol.stem}/captures/x", b"x",
                state="refused")  # a refused row: nothing copied
    _handover(client, vol.col)  # a waiting row: nothing sent
    resp = _as(client, "admin", "boss").post(f"/collections/{vol.col.id}/keep-on-unit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["kept_on_unit"] is True and resp.json()["pending"] == 0
    assert _kept(db_session, vol.col.id) is True
    assert [r.state for r in _rows(db_session, vol.col)] == ["waiting_for_upload"]
    [log] = _logs(db_session, "volume_kept_on_unit")
    assert log.actor == "boss" and log.subject == vol.col.name


@pytest.mark.unit
@pytest.mark.parametrize("state", ["verified", "uploaded", "pending"])
def test_keeping_a_volume_with_a_file_offsite_is_a_409(client, db_session, vol, state):
    vol.record(upload=False)
    vol.offsite(vol.col.records[0].images[0], "capture", f"{TENANT}/{vol.stem}/captures/x", b"x", state=state)
    _detail(_as(client, "admin").post(f"/collections/{vol.col.id}/keep-on-unit"), 409, "already_left_unit")
    assert _kept(db_session, vol.col.id) is False


@pytest.mark.unit
@pytest.mark.parametrize("state", ["ready", "sending", "failed", "delivered", "refused"])
def test_keeping_a_volume_with_a_manifest_sent_or_on_its_way_is_a_409(client, db_session, vol, state):
    import uuid
    from app.models.fisqua import FisquaOutbox
    vol.record(upload=False)
    now = datetime.now(timezone.utc)
    extra = {"refused": {"refusal": "fisqua"}, "delivered": {"delivered_at": now}}.get(state, {})
    db_session.add(FisquaOutbox(collection_id=vol.col.id, manifest_id=str(uuid.uuid4()), stem=vol.stem,
                                state=state, attempts=1, created_at=now, **extra))
    db_session.commit()
    _detail(_as(client, "admin").post(f"/collections/{vol.col.id}/keep-on-unit"), 409, "already_left_unit")
    assert _kept(db_session, vol.col.id) is False


@pytest.mark.unit
@pytest.mark.parametrize("state", ["superseded", "withdrawn"])
def test_an_ended_unsent_manifest_does_not_stop_a_keep(client, db_session, vol, state):
    import uuid
    from app.models.fisqua import FisquaOutbox
    vol.record(upload=False)
    db_session.add(FisquaOutbox(collection_id=vol.col.id, manifest_id=str(uuid.uuid4()), stem=vol.stem,
                                state=state, attempts=0, created_at=datetime.now(timezone.utc)))
    db_session.commit()
    assert _as(client, "admin").post(f"/collections/{vol.col.id}/keep-on-unit").status_code == 200


@pytest.mark.unit
def test_keeping_a_kept_volume_is_a_409(client, db_session):
    col = _kept_volume(db_session)
    _detail(_as(client, "admin").post(f"/collections/{col.id}/keep-on-unit"), 409, "already_kept_on_unit")
    assert _as(client, "admin").post("/collections/99999/keep-on-unit").status_code == 404


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_only_an_admin_keeps_a_volume(client, db_session, vol, role):
    assert _as(client, role).post(f"/collections/{vol.col.id}/keep-on-unit").status_code == 403
    assert _kept(db_session, vol.col.id) is False


@pytest.mark.unit
def test_the_volume_read_carries_the_flag(client, db_session):
    col = _kept_volume(db_session)
    assert _as(client, "reviewer").get(f"/collections/{col.id}").json()["kept_on_unit"] is True


# ---------------------------------------------------------------------------
# Fix round: F1, a file on its way
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_pending_row_left_by_a_crash_refuses_a_keep(client, db_session, vol):
    # A claimed key whose copy never finished: the next run copies it, so it is leaving.
    vol.record(upload=False)
    img = vol.col.records[0].images[0]
    vol.offsite(img, "capture", f"{TENANT}/{vol.stem}/captures/{img.filename}", b"x", state="pending")
    _detail(_as(client, "admin").post(f"/collections/{vol.col.id}/keep-on-unit"), 409, "already_left_unit")
    assert _kept(db_session, vol.col.id) is False


@pytest.mark.unit
def test_a_row_of_a_deleted_image_counts_by_its_key_under_the_volume(client, db_session, vol):
    from app.models.offsite_object import OffsiteObject
    vol.record(upload=False)
    db_session.add(OffsiteObject(record_image_id=None, kind="capture", local_sha1="0" * 40,
                                 object_key=f"{TENANT}/{vol.stem}/captures/gone.jpg"))
    db_session.commit()
    _detail(_as(client, "admin").post(f"/collections/{vol.col.id}/keep-on-unit"), 409, "already_left_unit")


@pytest.mark.unit
def test_a_row_of_a_deleted_image_under_another_stem_does_not_count(client, db_session, vol):
    from app.models.offsite_object import OffsiteObject
    vol.record(upload=False)
    db_session.add(OffsiteObject(record_image_id=None, kind="capture", local_sha1="0" * 40,
                                 object_key=f"{TENANT}/{vol.stem}-other/captures/gone.jpg"))
    db_session.commit()
    assert _as(client, "admin").post(f"/collections/{vol.col.id}/keep-on-unit").status_code == 200


from tests.unit.test_image_upload import _image as _upload_image, _volume as _upload_volume, remote  # noqa: E402,F401


@pytest.mark.unit
def test_a_keep_during_a_copy_in_flight_is_refused_and_the_copy_completes(
        client, db_session, remote, override_projects_root, monkeypatch):
    from app.core import image_upload
    col = _upload_volume(db_session)
    _upload_image(db_session, col, override_projects_root, "f1.jpg", b"jpeg one")
    answers = []
    real_upload = image_upload.upload_file

    def upload_with_a_keep(local, key):
        # The key is claimed and committed (a pending row) before the copy starts.
        answers.append(_as(client, "admin").post(f"/collections/{col.id}/keep-on-unit"))
        return real_upload(local, key)

    monkeypatch.setattr(image_upload, "upload_file", upload_with_a_keep)
    result = image_upload.run_image_upload(db_session)
    assert result["uploaded"] == 1, result
    _detail(answers[0], 409, "already_left_unit")
    assert _kept(db_session, col.id) is False


@pytest.mark.unit
def test_a_keep_committed_before_the_first_claim_stops_the_object(
        client, db_session, remote, override_projects_root, monkeypatch):
    from app.core import image_upload
    from app.models.offsite_object import OffsiteObject
    col = _upload_volume(db_session)
    _upload_image(db_session, col, override_projects_root, "f1.jpg", b"jpeg one")
    real_listed = image_upload.listed_object

    def listed_after_a_keep(key):
        # Between the staging of the file and its first offsite row.
        resp = _as(client, "admin").post(f"/collections/{col.id}/keep-on-unit")
        assert resp.status_code == 200, resp.text
        return real_listed(key)

    monkeypatch.setattr(image_upload, "listed_object", listed_after_a_keep)
    result = image_upload.run_image_upload(db_session)
    assert result["uploaded"] == 0 and remote.calls("copyto") == []
    assert [s["reason"] for s in result["skipped"]] == ["image changed while it was read"]
    db_session.expire_all()
    assert db_session.query(OffsiteObject).count() == 0
    assert _kept(db_session, col.id) is True


# ---------------------------------------------------------------------------
# F2: records do not cross between kept and not kept volumes
# ---------------------------------------------------------------------------

def _captured_record(db, collection_id, title="r1"):
    from app.models.record import Record
    rec = Record(title=title, collection_id=collection_id, status="captured", capture_mode="single",
                 sequence=1)
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def _plain_volume(db, name, stem, kept=False):
    col = _kept_volume(db, name, stem)
    _set_kept(db, col, kept)
    return col


@pytest.mark.unit
@pytest.mark.parametrize("from_kept, to_kept", [(True, False), (False, True)])
def test_move_records_between_a_kept_and_a_not_kept_volume_is_refused(client, db_session, override_projects_root,
                                                                     from_kept, to_kept):
    from app.models.record import Record
    src = _plain_volume(db_session, "Src", "xx-test-s1", kept=from_kept)
    dst = _plain_volume(db_session, "Dst", "xx-test-d1", kept=to_kept)
    rec = _captured_record(db_session, src.id)
    resp = _as(client, "operator").post(f"/collections/{src.id}/move-records",
                                        params={"target_collection_id": dst.id})
    _detail(resp, 409, "kept_on_unit_move")
    db_session.expire_all()
    assert db_session.get(Record, rec.id).collection_id == src.id


@pytest.mark.unit
def test_move_records_between_two_kept_volumes_goes_ahead(client, db_session, override_projects_root):
    src = _plain_volume(db_session, "Src", "xx-test-s1", kept=True)
    dst = _plain_volume(db_session, "Dst", "xx-test-d1", kept=True)
    _captured_record(db_session, src.id)
    resp = _as(client, "operator").post(f"/collections/{src.id}/move-records",
                                        params={"target_collection_id": dst.id})
    assert resp.status_code == 200, resp.text


@pytest.mark.unit
@pytest.mark.parametrize("from_kept, to_kept", [(True, False), (False, True)])
def test_patching_a_records_volume_across_the_kept_line_is_refused(client, db_session, from_kept, to_kept):
    from app.models.record import Record
    src = _plain_volume(db_session, "Src", "xx-test-s1", kept=from_kept)
    dst = _plain_volume(db_session, "Dst", "xx-test-d1", kept=to_kept)
    rec = _captured_record(db_session, src.id)
    _detail(_as(client, "operator").patch(f"/records/{rec.id}", json={"collection_id": dst.id}),
            409, "kept_on_unit_move")
    db_session.expire_all()
    assert db_session.get(Record, rec.id).collection_id == src.id


@pytest.mark.unit
def test_patching_a_record_out_of_a_kept_volume_or_into_one_from_a_project_is_refused(client, db_session):
    from app.models.record import Record
    kept = _plain_volume(db_session, "Kept", "xx-test-k1", kept=True)
    rec = _captured_record(db_session, kept.id)
    _detail(_as(client, "operator").patch(f"/records/{rec.id}", json={"project_id": kept.project_id}),
            409, "kept_on_unit_move")
    loose = Record(title="loose", project_id=kept.project_id, status="captured", capture_mode="single")
    db_session.add(loose)
    db_session.commit()
    _detail(_as(client, "operator").patch(f"/records/{loose.id}", json={"collection_id": kept.id}),
            409, "kept_on_unit_move")


@pytest.mark.unit
def test_add_record_to_project_out_of_a_kept_volume_is_refused(client, db_session):
    from app.models.record import Record
    kept = _plain_volume(db_session, "Kept", "xx-test-k1", kept=True)
    rec = _captured_record(db_session, kept.id)
    _detail(_as(client, "operator").post(f"/projects/{kept.project_id}/add_record/{rec.id}"),
            409, "kept_on_unit_move")
    db_session.expire_all()
    assert db_session.get(Record, rec.id).collection_id == kept.id


@pytest.mark.unit
def test_add_record_to_project_out_of_a_volume_not_kept_goes_ahead(client, db_session):
    plain = _plain_volume(db_session, "Plain", "xx-test-p1", kept=False)
    rec = _captured_record(db_session, plain.id)
    assert _as(client, "operator").post(f"/projects/{plain.project_id}/add_record/{rec.id}").status_code == 200


@pytest.mark.unit
def test_a_subcollection_of_a_kept_volume_is_created_kept(client, db_session):
    _unit(db_session, "backup")
    parent = _kept_volume(db_session)
    resp = _as(client, "operator").post("/collections/", json={
        "name": "Sub", "parent_collection_id": parent.id, "filename_stem": "xx-test-sub"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["kept_on_unit"] is True


@pytest.mark.unit
def test_a_subcollection_of_a_kept_volume_in_fisqua_mode_is_not_checked_with_fisqua(client, db_session, link):
    _unit(db_session, "fisqua")
    parent = _kept_volume(db_session)
    resp = _as(client, "admin").post("/collections/", json={
        "name": "Sub", "parent_collection_id": parent.id, "filename_stem": "xx-test-sub"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["kept_on_unit"] is True
    assert link.descriptions_asked == []


# ---------------------------------------------------------------------------
# F3: a switch committed while a volume is being created
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_switch_committed_before_the_insert_refuses_the_new_volume(client, db_session, monkeypatch):
    from app.core import unit_mode
    from app.models.collection import Collection
    _unit(db_session, "offline")  # projects hidden: the default project is chosen before the insert
    real_default = unit_mode.default_project

    def default_then_switch(db, actor):
        out = real_default(db, actor)
        _unit(db_session, "backup")  # another admin's switch commits meanwhile
        return out

    monkeypatch.setattr(unit_mode, "default_project", default_then_switch)
    resp = _create(client, "operator", _project(db_session, "P"))
    _detail(resp, 409, "mode_changed")
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
def test_a_switch_committed_during_a_list_import_rejects_the_volumes_after_it(
        client, db_session, reports_root, monkeypatch):
    from app.core import unit_mode
    from app.models.collection import Collection
    _unit(db_session, "offline")
    real_default = unit_mode.default_project

    def default_then_switch(db, actor):
        out = real_default(db, actor)
        _unit(db_session, "backup")
        return out

    monkeypatch.setattr(unit_mode, "default_project", default_then_switch)
    _as_importer("op", "operator")
    resp = _import(client, _list_with([_item()]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 0
    assert [r["reason"] for r in resp.json()["rejects"]] == ["mode_changed"]
    assert db_session.query(Collection).count() == 0


@pytest.mark.unit
def test_the_insert_reads_the_mode_with_the_settings_row_for_share(db_session, monkeypatch):
    # On SQLite FOR SHARE emits nothing; what is checked is that the read asks for it.
    from sqlalchemy.orm import Query

    from app.core import unit_mode
    _unit(db_session, "backup")
    asked = []
    real = Query.with_for_update

    def spy(self, *a, **kw):
        asked.append(kw)
        return real(self, *a, **kw)

    monkeypatch.setattr(Query, "with_for_update", spy)
    assert unit_mode.mode_for_insert(db_session) == "backup"
    assert asked == [{"read": True}]


# ---------------------------------------------------------------------------
# F4: the queue leaves kept volumes alone
# ---------------------------------------------------------------------------

from tests.unit.test_fisqua_queue import QueueTransport, _ok_ack, _qitem, _queue, _sync, synced  # noqa: E402,F401


@pytest.mark.unit
@pytest.mark.parametrize("withdrawn", [False, True])
def test_the_queue_skips_an_item_whose_volume_is_kept(db_session, synced, withdrawn):
    from app.models.collection import Collection
    col = _kept_volume(db_session, "Kept", "xx-test-901")
    transport = QueueTransport(queues=[_queue([_qitem("xx-test-901", withdrawn=withdrawn, sequence=7)])],
                               acks=[_ok_ack()])
    _sync(db_session, transport)
    [ack] = transport.acks_sent
    assert ack["applied"] == []
    assert [s["reason"] for s in ack["skipped"]] == ["kept_on_unit"]
    db_session.expire_all()
    kept = db_session.get(Collection, col.id)
    assert kept.sequence is None and kept.queue_hidden_at is None
    assert db_session.query(Collection).count() == 1


# ---------------------------------------------------------------------------
# F5: the released ids fit the log's detail
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_released_detail_keeps_every_id_while_they_fit_and_says_how_many_it_left_out():
    from app.core.unit_mode import DETAIL_MAX, released_detail
    assert released_detail([1, 2, 3]) == "3 volume(s) included on leaving offline mode: ids 1, 2, 3"
    ids = list(range(100000, 100200))
    detail = released_detail(ids)
    assert len(detail) <= DETAIL_MAX
    shown = detail.split("ids ", 1)[1].split(" …")[0].split(", ")
    left = int(detail.rsplit("… ", 1)[1].split(" ")[0])
    assert detail.startswith("200 volume(s)")
    assert shown == [str(i) for i in ids[:len(shown)]] and len(shown) + left == 200
    assert detail.endswith(f"… {left} more")


@pytest.mark.unit
def test_a_switch_releasing_many_volumes_logs_a_detail_that_fits(client, db_session, link):
    from app.models.collection import Collection
    from app.models.project import Project
    _unit(db_session, "offline")
    p = Project(name="Many")
    db_session.add(p)
    db_session.commit()
    db_session.add_all([Collection(name=f"T{i}", project_id=p.id, filename_stem=f"xx-many-{i}", kept_on_unit=True)
                        for i in range(150)])
    db_session.commit()
    resp = _as(client, "admin").put("/system/unit", json={"mode": "backup", "keptVolumes": "include"})
    assert resp.status_code == 200, resp.text
    [log] = _logs(db_session, "kept_volumes_released")
    assert log.detail.startswith("150 volume(s)") and "more" in log.detail and len(log.detail) <= 500


# ---------------------------------------------------------------------------
# F7: the missing cases
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_release_after_a_kept_volumes_stem_was_changed_checks_the_new_code(client, db_session, link):
    _unit(db_session, "fisqua")
    col = _kept_volume(db_session, stem="xx-test-old")
    resp = _as(client, "admin").patch(f"/collections/{col.id}", json={"filename_stem": "xx-test-new"})
    assert resp.status_code == 200, resp.text
    assert link.descriptions_asked == []
    link.description_answers = [_result("unknown_code", status_code=404)]
    _detail(_as(client, "admin").post(f"/collections/{col.id}/release-from-unit"), 422, "unknown_reference_code")
    assert link.descriptions_asked == ["xx-test-new"]
    assert _kept(db_session, col.id) is True


@pytest.mark.unit
def test_a_switch_refused_as_mode_changed_leaves_the_flags(client, db_session, link, monkeypatch):
    from app.core import fisqua_queue, unit_mode
    _unit(db_session, "offline")
    col = _kept_volume(db_session)

    def racing_put(db, mode, transport=None, now=None, **kw):
        _unit(db_session, "backup")  # another admin's switch commits meanwhile
        return True, None

    monkeypatch.setattr(fisqua_queue, "send_operators_with_mode", racing_put)
    resp = _as(client, "admin").put("/system/unit", json={"mode": "fisqua", "keptVolumes": "include"})
    assert resp.status_code == 409, resp.text
    assert _problem_codes(resp) == ["mode_changed"]
    assert unit_mode.current(db_session).mode == "backup"
    assert _kept(db_session, col.id) is True
    assert _logs(db_session, "kept_volumes_released") == []


@pytest.mark.unit
def test_a_first_claim_takes_the_volume_row_for_share_after_the_record(db_session, vol, monkeypatch):
    # The lock that makes a keep and a first claim take turns. SQLite emits no
    # lock clause, so what is checked is the order the claim asks for them in.
    from sqlalchemy.orm import Query

    from app.core import image_upload
    vol.record(upload=False)
    img = vol.col.records[0].images[0]
    state = image_upload._image_state(db_session, img.id)
    asked = []
    real = Query.with_for_update

    def spy(self, *a, **kw):
        asked.append((self.column_descriptions[0]["entity"].__name__, kw))
        return real(self, *a, **kw)

    monkeypatch.setattr(Query, "with_for_update", spy)
    image_upload._claim(db_session, None, image_upload.Work(img.id, "capture", vol.stem, img.file_path),
                        f"{TENANT}/{vol.stem}/captures/{img.filename}", state, local_sha1="0" * 40)
    assert asked == [("Record", {}), ("Collection", {"read": True})]


# ---------------------------------------------------------------------------
# Round 3: G1, the move check locks the volumes; G2, re-parenting
# ---------------------------------------------------------------------------

def _spy_locks(monkeypatch):
    from sqlalchemy.orm import Query
    asked = []
    real = Query.with_for_update

    def spy(self, *a, **kw):
        entity = self.column_descriptions[0].get("entity")
        asked.append((entity.__name__ if entity is not None else None, kw))
        return real(self, *a, **kw)

    monkeypatch.setattr(Query, "with_for_update", spy)
    return asked


@pytest.mark.unit
def test_the_move_check_locks_both_volumes_in_id_order(db_session, monkeypatch):
    from app.core import kept_volumes
    a = _plain_volume(db_session, "A", "xx-test-a1")
    b = _plain_volume(db_session, "B", "xx-test-b1")
    asked = _spy_locks(monkeypatch)
    kept_volumes.refuse_kept_move(db_session, b.id, a.id)  # source b, target a (lower id)
    assert asked == [("Collection", {}), ("Collection", {"read": True})]
    asked.clear()
    kept_volumes.refuse_kept_move(db_session, a.id, b.id)
    assert asked == [("Collection", {"read": True}), ("Collection", {})]
    asked.clear()
    kept_volumes.refuse_kept_move(db_session, a.id, None)
    assert asked == [("Collection", {"read": True})]


@pytest.mark.unit
def test_a_patch_move_takes_the_record_before_the_volumes(client, db_session, monkeypatch):
    src = _plain_volume(db_session, "Src", "xx-test-s1")
    dst = _plain_volume(db_session, "Dst", "xx-test-d1")
    rec = _captured_record(db_session, src.id)
    asked = _spy_locks(monkeypatch)
    resp = _as(client, "operator").patch(f"/records/{rec.id}", json={"collection_id": dst.id})
    assert resp.status_code == 200, resp.text
    entities = [e for e, _ in asked]
    assert entities.index("Record") < entities.index("Collection")
    assert ("Collection", {"read": True}) in asked


@pytest.mark.unit
def test_a_re_parent_between_kept_volumes_goes_ahead(client, db_session):
    moving = _plain_volume(db_session, "Moving", "xx-test-m1", kept=True)
    parent = _plain_volume(db_session, "Parent", "xx-test-p1", kept=True)
    resp = _as(client, "admin").patch(f"/collections/{moving.id}", json={"parent_collection_id": parent.id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_collection_id"] == parent.id


@pytest.mark.unit
def test_the_re_parent_check_reads_the_new_parent_for_share(client, db_session, monkeypatch):
    moving = _plain_volume(db_session, "Moving", "xx-test-m1")
    parent = _plain_volume(db_session, "Parent", "xx-test-p1")
    asked = _spy_locks(monkeypatch)
    assert _as(client, "admin").patch(f"/collections/{moving.id}",
                                      json={"parent_collection_id": parent.id}).status_code == 200
    # The moving volume's own row first (FOR UPDATE: the PATCH may change its unique stem or
    # external_id), then the new parent's FOR SHARE.
    assert asked[:2] == [("Collection", {}), ("Collection", {"read": True})]


# ---------------------------------------------------------------------------
# Round 4: kept applies to a volume and everything inside it (ruling (a))
# ---------------------------------------------------------------------------

def _child(db, parent, name, stem, kept=False):
    from app.models.collection import Collection
    col = Collection(name=name, parent_collection_id=parent.id, filename_stem=stem, kept_on_unit=kept)
    db.add(col)
    db.commit()
    db.refresh(col)
    return col


def _tree(db, kept=False, child_kept=None, grandchild_kept=None):
    """A top volume, a child and a grandchild, flagged as asked (the children like the top by default)."""
    top = _plain_volume(db, "Top", "xx-test-t1", kept=kept)
    child = _child(db, top, "Child", "xx-test-c1", kept if child_kept is None else child_kept)
    grandchild = _child(db, child, "Grandchild", "xx-test-g1",
                        kept if grandchild_kept is None else grandchild_kept)
    return top, child, grandchild


# FOR NO KEY UPDATE: conflicts with FOR SHARE and FOR UPDATE, not with the
# FOR KEY SHARE a foreign-key check takes.
NO_KEY_UPDATE = {"key_share": True}


def _spy_locked_ids(monkeypatch):
    """(collection id, kwargs) for every row lock a query asks for, in the order asked."""
    from sqlalchemy.orm import Query
    asked = []
    real = Query.with_for_update

    def spy(self, *a, **kw):
        try:
            locked = self.whereclause.right.value
        except AttributeError:
            locked = None
        asked.append((locked, kw))
        return real(self, *a, **kw)

    monkeypatch.setattr(Query, "with_for_update", spy)
    return asked


@pytest.mark.unit
def test_keep_cascades_to_children_and_grandchildren(client, db_session):
    top, child, grandchild = _tree(db_session)
    resp = _as(client, "admin", "boss").post(f"/collections/{top.id}/keep-on-unit")
    assert resp.status_code == 200, resp.text
    assert [_kept(db_session, c.id) for c in (top, child, grandchild)] == [True, True, True]
    [log] = _logs(db_session, "volume_kept_on_unit")
    assert log.subject == top.name and "2 volume(s) inside it kept with it" in log.detail


@pytest.mark.unit
def test_keep_is_refused_as_a_whole_when_a_descendant_has_left_the_unit(client, db_session):
    import uuid
    from app.models.fisqua import FisquaOutbox
    top, child, grandchild = _tree(db_session)
    db_session.add(FisquaOutbox(collection_id=grandchild.id, manifest_id=str(uuid.uuid4()), stem="xx-test-g1",
                                state="ready", attempts=1, created_at=datetime.now(timezone.utc)))
    db_session.commit()
    _detail(_as(client, "admin").post(f"/collections/{top.id}/keep-on-unit"), 409, "already_left_unit")
    assert [_kept(db_session, c.id) for c in (top, child, grandchild)] == [False, False, False]
    assert _logs(db_session, "volume_kept_on_unit") == []


@pytest.mark.unit
def test_keep_leaves_descendants_already_kept_as_they_are(client, db_session):
    top, child, grandchild = _tree(db_session, child_kept=True, grandchild_kept=True)
    resp = _as(client, "admin").post(f"/collections/{top.id}/keep-on-unit")
    assert resp.status_code == 200, resp.text
    assert [_kept(db_session, c.id) for c in (top, child, grandchild)] == [True, True, True]
    [log] = _logs(db_session, "volume_kept_on_unit")
    assert "0 volume(s) inside it kept with it" in log.detail


@pytest.mark.unit
def test_release_under_a_kept_parent_is_refused(client, db_session):
    top, child, grandchild = _tree(db_session, kept=True)
    _detail(_as(client, "admin").post(f"/collections/{child.id}/release-from-unit"), 409, "kept_parent")
    assert [_kept(db_session, c.id) for c in (top, child, grandchild)] == [True, True, True]
    assert _logs(db_session, "volume_released_from_unit") == []


@pytest.mark.unit
def test_release_cascades_to_every_kept_descendant(client, db_session):
    _unit(db_session, "backup")
    top, child, grandchild = _tree(db_session, kept=True)
    resp = _as(client, "admin", "boss").post(f"/collections/{top.id}/release-from-unit")
    assert resp.status_code == 200, resp.text
    assert [_kept(db_session, c.id) for c in (top, child, grandchild)] == [False, False, False]
    [log] = _logs(db_session, "volume_released_from_unit")
    assert log.subject == top.name and "2 volume(s) inside it released with it" in log.detail


@pytest.mark.unit
def test_a_kept_volume_inside_one_not_kept_can_be_released(client, db_session):
    _unit(db_session, "backup")
    top, child, grandchild = _tree(db_session, kept=False, child_kept=True, grandchild_kept=True)
    resp = _as(client, "admin").post(f"/collections/{child.id}/release-from-unit")
    assert resp.status_code == 200, resp.text
    assert [_kept(db_session, c.id) for c in (top, child, grandchild)] == [False, False, False]


@pytest.mark.unit
def test_a_fisqua_release_where_one_descendants_check_fails_leaves_every_flag(client, db_session, link):
    _unit(db_session, "fisqua")
    top, child, grandchild = _tree(db_session, kept=True)
    link.description_answers = [_found("xx-test-t1"), _result("unknown_code", status_code=404)]
    resp = _as(client, "admin").post(f"/collections/{top.id}/release-from-unit")
    _detail(resp, 422, "unknown_reference_code")
    assert link.descriptions_asked == ["xx-test-t1", "xx-test-c1"]
    assert [_kept(db_session, c.id) for c in (top, child, grandchild)] == [True, True, True]
    assert _logs(db_session, "volume_released_from_unit") == []


@pytest.mark.unit
def test_a_fisqua_release_checks_every_volume_it_releases(client, db_session, link):
    _unit(db_session, "fisqua")
    top, child, grandchild = _tree(db_session, kept=True)
    link.description_answers = [_found("xx-test-t1"), _found("xx-test-c1"), _found("xx-test-g1")]
    resp = _as(client, "admin").post(f"/collections/{top.id}/release-from-unit")
    assert resp.status_code == 200, resp.text
    assert link.descriptions_asked == ["xx-test-t1", "xx-test-c1", "xx-test-g1"]
    assert [_kept(db_session, c.id) for c in (top, child, grandchild)] == [False, False, False]


@pytest.mark.unit
def test_a_not_kept_volume_cannot_move_into_a_kept_one(client, db_session):
    from app.models.collection import Collection
    moving = _plain_volume(db_session, "Moving", "xx-test-m1", kept=False)
    parent = _plain_volume(db_session, "Parent", "xx-test-p1", kept=True)
    _detail(_as(client, "admin").patch(f"/collections/{moving.id}", json={"parent_collection_id": parent.id}),
            409, "kept_on_unit_move")
    db_session.expire_all()
    assert db_session.get(Collection, moving.id).parent_collection_id is None


@pytest.mark.unit
@pytest.mark.parametrize("moving_kept, parent_kept", [(True, False), (False, False), (True, True)])
def test_a_re_parent_that_keeps_the_invariant_goes_ahead(client, db_session, moving_kept, parent_kept):
    moving = _plain_volume(db_session, "Moving", "xx-test-m1", kept=moving_kept)
    parent = _plain_volume(db_session, "Parent", "xx-test-p1", kept=parent_kept)
    resp = _as(client, "admin").patch(f"/collections/{moving.id}", json={"parent_collection_id": parent.id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_collection_id"] == parent.id
    assert _kept(db_session, moving.id) is moving_kept


@pytest.mark.unit
@pytest.mark.parametrize("parent_kept", [True, False])
def test_a_kept_subcollection_may_move_to_the_top_level(client, db_session, parent_kept):
    parent = _plain_volume(db_session, "Parent", "xx-test-p1", kept=parent_kept)
    sub = _child(db_session, parent, "Sub", "xx-test-sub", kept=True)
    resp = _as(client, "admin").patch(f"/collections/{sub.id}", json={"parent_collection_id": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_collection_id"] is None and _kept(db_session, sub.id) is True


@pytest.mark.unit
@pytest.mark.parametrize("moving_first", [True, False])
def test_the_re_parent_locks_the_volume_and_its_new_parent_in_id_order(client, db_session, monkeypatch,
                                                                       moving_first):
    a = _plain_volume(db_session, "A", "xx-test-a1")
    b = _plain_volume(db_session, "B", "xx-test-b1")
    moving, parent = (a, b) if moving_first else (b, a)
    asked = _spy_locked_ids(monkeypatch)
    assert _as(client, "admin").patch(f"/collections/{moving.id}",
                                      json={"parent_collection_id": parent.id}).status_code == 200
    expected = sorted([(moving.id, {}), (parent.id, {"read": True})])
    assert asked == expected


@pytest.mark.unit
def test_keep_locks_the_volume_and_its_descendants_in_id_order(db_session, monkeypatch):
    from app.core import kept_volumes
    # Ids made out of tree order: the grandchild's id is below the child's.
    top = _plain_volume(db_session, "Top", "xx-test-t1")
    other = _plain_volume(db_session, "Other", "xx-test-o1")
    grandchild = _child(db_session, other, "Grandchild", "xx-test-g1")
    child = _child(db_session, top, "Child", "xx-test-c1")
    grandchild.parent_collection_id = child.id
    db_session.commit()
    asked = _spy_locked_ids(monkeypatch)
    kept_volumes.keep(db_session, top.id, "boss")
    assert asked == [(i, NO_KEY_UPDATE) for i in sorted([top.id, child.id, grandchild.id])]


@pytest.mark.unit
def test_release_locks_the_parent_for_share_and_the_volumes_in_one_id_order_pass(db_session, monkeypatch):
    from app.core import kept_volumes
    parent = _plain_volume(db_session, "Parent", "xx-test-p0", kept=False)
    # The volume released has an id below one of its descendants and above its parent's,
    # and one descendant id below its own.
    low = _plain_volume(db_session, "Low", "xx-test-l0")
    volume = _child(db_session, parent, "Volume", "xx-test-v1", kept=True)
    low.project_id = None
    low.parent_collection_id = volume.id
    low.kept_on_unit = True
    db_session.commit()
    high = _child(db_session, volume, "High", "xx-test-h1", kept=True)
    asked = _spy_locked_ids(monkeypatch)
    kept_volumes.release(db_session, volume.id, "boss")
    expected = sorted([(parent.id, {"read": True}), (low.id, NO_KEY_UPDATE), (volume.id, NO_KEY_UPDATE),
                       (high.id, NO_KEY_UPDATE)])
    assert asked == expected
    assert [_kept(db_session, c.id) for c in (low, volume, high)] == [False, False, False]


@pytest.mark.unit
def test_a_descendant_added_between_the_read_and_the_locks_is_kept_on_the_retry(db_session, monkeypatch):
    from app.core import kept_volumes
    top = _plain_volume(db_session, "Top", "xx-test-t1")
    real = kept_volumes._subtree
    added = []

    def subtree_then_a_new_child(db, root_id):
        out = real(db, root_id)
        if not added:  # another session creates a child of the volume meanwhile
            added.append(_child(db_session, top, "Late", "xx-test-late"))
        return out

    monkeypatch.setattr(kept_volumes, "_subtree", subtree_then_a_new_child)
    kept_volumes.keep(db_session, top.id, "boss")
    assert _kept(db_session, top.id) is True and _kept(db_session, added[0].id) is True


@pytest.mark.unit
@pytest.mark.parametrize("action", ["keep", "release"])
def test_a_descendant_set_that_keeps_changing_is_a_409_after_the_retries(db_session, monkeypatch, action):
    from app.core import kept_volumes
    top = _plain_volume(db_session, "Top", "xx-test-t1", kept=action == "release")
    real = kept_volumes._subtree
    calls = []

    def ever_changing(db, root_id):
        calls.append(root_id)
        out = dict(real(db, root_id))
        out[10_000 + len(calls)] = (False, None, None)  # a volume that is never there twice
        return out

    monkeypatch.setattr(kept_volumes, "_subtree", ever_changing)
    with pytest.raises(kept_volumes.KeepRefused) as refused:
        getattr(kept_volumes, action)(db_session, top.id, "boss")
    assert refused.value.status == 409 and refused.value.code == "kept_volumes_changed"
    assert len(calls) == 2 * kept_volumes.LOCK_ATTEMPTS
    assert _kept(db_session, top.id) is (action == "release")


@pytest.mark.unit
def test_the_include_locks_the_kept_volumes_in_id_order_before_releasing_them(db_session, monkeypatch):
    from sqlalchemy.orm import Query

    from app.core import unit_mode
    a = _plain_volume(db_session, "A", "xx-test-a1", kept=True)
    b = _plain_volume(db_session, "B", "xx-test-b1", kept=True)
    asked = []
    real = Query.with_for_update

    def spy(self, *a_, **kw):
        asked.append((str(self.statement), kw))
        return real(self, *a_, **kw)

    monkeypatch.setattr(Query, "with_for_update", spy)
    unit_mode._decide_kept(db_session, unit_mode.KEPT_INCLUDE, "boss")
    [(sql, kw)] = asked
    assert kw == NO_KEY_UPDATE and "ORDER BY collections.id" in sql
    db_session.commit()
    assert _kept(db_session, a.id) is False and _kept(db_session, b.id) is False


# ---------------------------------------------------------------------------
# Round 5
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_release_of_the_parent_committed_before_the_insert_refuses_the_new_volume(
        client, db_session, monkeypatch):
    from app.api import collections as collections_api
    from app.models.collection import Collection
    _unit(db_session, "backup")
    parent = _kept_volume(db_session)
    real = collections_api._kept_parent

    def read_then_release(db, parent_id, lock=False):
        if lock:  # an admin's release of the parent commits between the two reads
            _set_kept(db_session, db_session.get(Collection, parent_id), False)
        return real(db, parent_id, lock=lock)

    monkeypatch.setattr(collections_api, "_kept_parent", read_then_release)
    resp = _as(client, "operator").post("/collections/", json={
        "name": "Sub", "parent_collection_id": parent.id, "filename_stem": "xx-test-sub"})
    _detail(resp, 409, "mode_changed")
    assert db_session.query(Collection).filter(Collection.name == "Sub").count() == 0


@pytest.mark.unit
def test_a_keep_of_the_parent_committed_before_the_insert_refuses_the_new_volume(
        client, db_session, monkeypatch):
    from app.api import collections as collections_api
    from app.models.collection import Collection
    _unit(db_session, "backup")
    parent = _plain_volume(db_session, "Parent", "xx-test-p1")
    real = collections_api._kept_parent

    def read_then_keep(db, parent_id, lock=False):
        if lock:
            _set_kept(db_session, db_session.get(Collection, parent_id), True)
        return real(db, parent_id, lock=lock)

    monkeypatch.setattr(collections_api, "_kept_parent", read_then_keep)
    # An admin asks for a kept volume: the flag agrees, the parent's read does not.
    resp = _as(client, "admin").post("/collections/", json={
        "name": "Sub", "parent_collection_id": parent.id, "filename_stem": "xx-test-sub", "kept_on_unit": True})
    _detail(resp, 409, "mode_changed")


@pytest.mark.unit
def test_a_keep_refused_for_a_sub_volume_says_a_volume_inside_it(client, db_session):
    import uuid
    from app.models.fisqua import FisquaOutbox
    top, child, grandchild = _tree(db_session)
    db_session.add(FisquaOutbox(collection_id=child.id, manifest_id=str(uuid.uuid4()), stem="xx-test-c1",
                                state="ready", attempts=1, created_at=datetime.now(timezone.utc)))
    db_session.commit()
    detail = _detail(_as(client, "admin").post(f"/collections/{top.id}/keep-on-unit"), 409, "already_left_unit")
    assert "or of a volume inside it" in detail["message"]


@pytest.mark.unit
def test_a_release_checks_a_code_changed_after_the_check_again(db_session):
    from app.core import kept_volumes
    from app.models.collection import Collection
    col = _kept_volume(db_session, stem="xx-test-old")
    checked = []

    def check(stem, external_id):
        checked.append(stem)
        if len(checked) == 1:  # an admin's PATCH of the stem commits after the check
            row = db_session.get(Collection, col.id)
            row.filename_stem = "xx-test-new"
            db_session.commit()

    kept_volumes.release(db_session, col.id, "boss", check=check)
    assert checked == ["xx-test-old", "xx-test-new"]
    assert _kept(db_session, col.id) is False


@pytest.mark.unit
def test_a_release_whose_parent_changed_before_the_locks_reads_the_new_parent(db_session, monkeypatch):
    from app.core import kept_volumes
    from app.models.collection import Collection
    old = _plain_volume(db_session, "Old", "xx-test-o1", kept=False)
    new = _plain_volume(db_session, "New", "xx-test-n1", kept=True)
    volume = _child(db_session, old, "Volume", "xx-test-v1", kept=True)
    real = kept_volumes._subtree
    calls = []

    def subtree_then_move(db, root_id):
        calls.append(root_id)
        if len(calls) == 1:  # moved under a kept parent meanwhile
            row = db_session.get(Collection, volume.id)
            row.parent_collection_id = new.id
            db_session.commit()
        return real(db, root_id)

    monkeypatch.setattr(kept_volumes, "_subtree", subtree_then_move)
    with pytest.raises(kept_volumes.KeepRefused) as refused:
        kept_volumes.release(db_session, volume.id, "boss")
    assert refused.value.code == "kept_parent"
    assert _kept(db_session, volume.id) is True


@pytest.mark.unit
def test_a_release_whose_parent_was_kept_before_the_locks_is_refused(db_session, monkeypatch):
    from app.core import kept_volumes
    from app.models.collection import Collection
    parent = _plain_volume(db_session, "Parent", "xx-test-p1", kept=False)
    volume = _child(db_session, parent, "Volume", "xx-test-v1", kept=True)
    real = kept_volumes._subtree
    calls = []

    def subtree_then_keep_the_parent(db, root_id):
        calls.append(root_id)
        if len(calls) == 1:  # an admin's keep of the parent commits meanwhile
            row = db_session.get(Collection, parent.id)
            row.kept_on_unit = True
            db_session.commit()
        return real(db, root_id)

    monkeypatch.setattr(kept_volumes, "_subtree", subtree_then_keep_the_parent)
    with pytest.raises(kept_volumes.KeepRefused) as refused:
        kept_volumes.release(db_session, volume.id, "boss")
    assert refused.value.code == "kept_parent"
    assert len(calls) == 2  # refused under the locks, not on a retry
    assert _kept(db_session, volume.id) is True


# ---------------------------------------------------------------------------
# Round 6
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_keep_committed_while_the_manifest_is_built_leaves_the_row_waiting(client, db_session, vol, link,
                                                                             monkeypatch):
    from app.core import capture_manifest
    from app.models.collection import Collection
    vol.record(status="captured")  # its objects verified, as if they had been copied
    _handover(client, vol.col)
    real = capture_manifest.build_capture_manifest

    def build_then_keep(db, collection, **kw):
        out = real(db, collection, **kw)
        _set_kept(db_session, db_session.get(Collection, vol.col.id))  # an admin's keep commits meanwhile
        return out

    monkeypatch.setattr(capture_manifest, "build_capture_manifest", build_then_keep)
    asked = _spy_locked_ids(monkeypatch)
    transport = FakeTransport(sends=[_delivered()])
    _run(db_session, transport)
    assert (vol.col.id, {"read": True}) in asked
    assert transport.sent == []
    assert [r.state for r in _rows(db_session, vol.col)] == ["waiting_for_upload"]


@pytest.mark.unit
def test_post_collections_reads_the_parent_for_share_before_the_insert(client, db_session, monkeypatch):
    _unit(db_session, "backup")
    from sqlalchemy.orm import Query
    parent = _kept_volume(db_session)
    asked = []
    real = Query.with_for_update

    def spy(self, *a, **kw):  # the collections rows only: unit_settings' id may equal the parent's
        entity = self.column_descriptions[0].get("entity")
        if entity is not None and entity.__name__ == "Collection":
            asked.append((self.whereclause.right.value, kw))
        return real(self, *a, **kw)

    monkeypatch.setattr(Query, "with_for_update", spy)
    resp = _as(client, "operator").post("/collections/", json={
        "name": "Sub", "parent_collection_id": parent.id, "filename_stem": "xx-test-sub"})
    assert resp.status_code == 201, resp.text
    assert asked == [(parent.id, {"read": True})]
