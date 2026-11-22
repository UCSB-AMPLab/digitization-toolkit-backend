"""Nothing of the testing space leaves the unit (NEH-391).

A unit holds testing material with distinctive strings (the project's
display and stored names, the volume's and sub-volume's names and stems, a
record title, an uploaded file's name, the testing account's username)
beside real material. In backup mode and in fisqua mode, with the unit
paired (Fisqua's environment, token and tenant set) and the offsite remote
configured, every code path that sends anything outside the unit, or
queues it to be sent, is run:

* the offsite image upload (image_upload.run_image_upload), through a real
  rclone against a local directory that stands for the bucket
  (tests/unit/test_image_upload.py's `remote`);
* the handover (POST /collections/{id}/promote) and the Fisqua outbox
  (fisqua_outbox.run_fisqua_outbox);
* the status poll and the decisions pull (fisqua_outbox.poll_fisqua_status);
* the operators list (fisqua_queue.send_operators);
* the queue sync and its acknowledgement (fisqua_queue.sync_fisqua_queue);
* the reference code lookup (POST /collections/ in fisqua mode) and the
  description search (GET /fisqua/descriptions);
* pairing by a code and its renewal (app.core.fisqua_pairing), with the
  HTTPS client the service builds;
* freeing and restoring a volume's files (app.core.offsite_free), which a
  testing volume, being kept, never reaches;
* the catalogue backup (app.core.catalogue_backup): the one accepted
  exception. It is a full dump of the database, encrypted to the unit's
  recipient, so it holds the testing rows like every other row; the test
  asserts only that the dump is the full one, as built on a unit with no
  testing material.

Every HTTPS request to Fisqua is taken at the wire: httpx.Client is built
on an httpx.MockTransport that records the method, the URL, the headers
and the body, and answers as Fisqua would. Every rclone call is recorded
by the wrapper script, and the bucket is a directory whose files are read
afterwards. The queued rows are every row of the outbox and offsite tables,
and every file the unit keeps in its data directory for a later send.
No testing string, and no testing id named with its kind, appears in any
of them.

    python -m pytest tests/unit/test_testing_space_isolation.py

Version: 0.3.0
"""

import json
import re
from pathlib import Path

import httpx
import pytest

from tests.unit.test_image_upload import remote  # noqa: F401  (fixture)
from tests.unit.test_testing_space_capture import Unit, _unit, backend  # noqa: F401  (backend is a fixture)

# Every testing string carries one of these, in any capitals.
NEEDLES = ("zzprueba", "captua-testing")
TESTING_USERNAME = "zzpruebacuenta"
TOKEN = "5e0c9a7b1d3f42e8a6b0c4d2e9f1a3b5c7d9e0f2a4b6c8d0e1f3a5b7c9d1e3f5"
SECRET = "ab" * 32
REAL_STEM = "xx-real-1"
QUEUE_CODE = "xx-real-9"
LOOKUP_CODE = "xx-real-2"

# The senders, each with how this test reaches it. The catalogue backup is
# the one exception: a full dump, encrypted, which holds every row.
SENDERS = {
    "image_upload": "run_image_upload, real rclone to a local directory remote",
    "handover": "POST /collections/{id}/promote, and the testing volume's refused",
    "fisqua_outbox": "run_fisqua_outbox: POST manifests",
    "status_poll": "poll_fisqua_status: GET volumes/status",
    "decisions_pull": "poll_fisqua_status on a status offering a decisionsId: GET volumes/{ref}/decisions",
    "operators": "send_operators: PUT operators",
    "queue_sync": "sync_fisqua_queue: GET queue, POST queue/ack",
    "reference_lookup": "POST /collections/ by an admin in fisqua mode: GET descriptions/{ref}",
    "description_search": "GET /fisqua/descriptions: GET descriptions?q=",
    "pairing": "PairingRegistry.begin, step, renew: POST pairing/codes, POST pairing/collect",
    "offsite_free": "plan_free, start_free and restore_volume on the testing volume: refused, nothing listed",
    "catalogue_backup": "EXCEPTION: the full encrypted dump; dump_argv is the one built before",
}


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------

class Wire:
    """Every HTTPS request the unit makes, answered as Fisqua answers it."""

    def __init__(self):
        self.requests = []
        self.manifest_ids = []

    def sent_to(self, path_part):
        return [r for r in self.requests if path_part in r["url"]]

    def handler(self, request: httpx.Request) -> httpx.Response:
        from app.core import fisqua_contract as fc
        body = request.content.decode("utf-8", "replace")
        self.requests.append({"method": request.method, "url": str(request.url),
                              "headers": {k: v for k, v in request.headers.items()
                                          if k.lower() != "authorization"},
                              "body": body})
        path = request.url.path
        if request.method == "POST" and path == fc.MANIFESTS_PATH:
            manifest = json.loads(body)
            self.manifest_ids.append(manifest.get("id"))
            return httpx.Response(201, json={"volumeId": "fq-1", "manifestId": manifest.get("id"),
                                             "state": "received"})
        if request.method == "GET" and path == fc.STATUS_PATH:
            refs = request.url.params.get(fc.STATUS_REFS_PARAM, "").split(fc.STATUS_REFS_SEPARATOR)
            return httpx.Response(200, json={"volumes": [
                {"ref": ref, "volumeId": "fq-1", "manifestId": self.manifest_ids[-1] if self.manifest_ids else None,
                 "state": "in_capture_qc", "enteredAt": None, "problems": [], "decisionsId": "d-1"}
                for ref in refs if ref]})
        if request.method == "GET" and path.endswith("/decisions"):
            return httpx.Response(404, json={"rule": fc.RULE_NO_DECISIONS})
        if request.method == "PUT" and path == fc.OPERATORS_PATH:
            return httpx.Response(200, json={})
        if request.method == "GET" and path == fc.QUEUE_PATH:
            return httpx.Response(200, headers={fc.ETAG_HEADER: '"r1"'}, json={
                "revision": "r1", "listName": "Cola", "items": [{
                    "itemId": "00000000-0000-4000-8000-000000000009", "referenceCode": QUEUE_CODE,
                    "filenameStem": QUEUE_CODE, "sequence": 1, "title": "Tomo de la cola",
                    "dates": {"display": "1790", "start": None, "end": None}, "extent": None,
                    "containerPath": [], "action": "full-digitisation", "notes": None,
                    "assignee": None, "withdrawn": False}]})
        if request.method == "POST" and path == fc.QUEUE_ACK_PATH:
            return httpx.Response(200, json={})
        if request.method == "GET" and path == fc.DESCRIPTION_SEARCH_PATH:
            return httpx.Response(200, json={"search": {"results": [], "total": 0}})
        if request.method == "GET" and path.startswith(fc.DESCRIPTION_SEARCH_PATH + "/"):
            ref = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"referenceCode": ref, "title": "T", "level": "file",
                                             "dates": {"display": None, "start": None, "end": None}})
        if request.method == "POST" and path == fc.PAIRING_CODES_PATH:
            return httpx.Response(201, json={fc.F_CODE: "ABCD-1234", fc.F_COLLECT_SECRET: SECRET,
                                             fc.F_EXPIRES_IN_SECONDS: 600, fc.F_POLL_SECONDS: 5})
        if request.method == "POST" and path == fc.PAIRING_COLLECT_PATH:
            return httpx.Response(404, json={fc.F_STATE: fc.PAIRING_UNKNOWN})
        return httpx.Response(500, json={"unexpected": path})


@pytest.fixture
def wire(monkeypatch):
    """httpx.Client built on a MockTransport that records every request (the test client is built before)."""
    recorder = Wire()
    real_client = httpx.Client
    mock = httpx.MockTransport(recorder.handler)

    def client(*args, **kwargs):
        kwargs["transport"] = mock
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return recorder


@pytest.fixture
def paired(remote, monkeypatch):  # noqa: F811
    """The unit paired with Fisqua (staging) and its offsite remote configured: everything set but the mode."""
    from pydantic import SecretStr

    from app.core import config, fisqua_outbox, fisqua_queue
    monkeypatch.setattr(config.settings, "FISQUA_ENV", "staging")
    monkeypatch.setattr(config.settings, "FISQUA_BASE_URL", "")
    monkeypatch.setattr(config.settings, "FISQUA_TOKEN", SecretStr(TOKEN))
    monkeypatch.setattr(fisqua_outbox, "call_budget", fisqua_outbox.CallBudget())
    monkeypatch.setattr(fisqua_outbox, "_status_cursor", None)
    monkeypatch.setattr(fisqua_outbox, "_decisions_cursor", None)
    monkeypatch.setattr(fisqua_queue, "_operators", fisqua_queue._OperatorsState(due=True))
    return remote


# ---------------------------------------------------------------------------
# The unit
# ---------------------------------------------------------------------------

class Material:
    """The testing material's ids, by kind, and its rows' directories."""

    def __init__(self):
        self.ids = {"project": set(), "collection": set(), "record": set(), "image": set()}


def _testing_material(unit) -> Material:
    """A testing project, a volume and a sub-volume, captures in both, a titled record, an upload and a missing page."""
    from app.models.record import RecordImage
    from app.models.unit_settings import UnitSettings
    from app.models.user import User

    account = unit.db.get(User, unit.users["testing"].id)
    account.username = TESTING_USERNAME
    settings = unit.db.get(UnitSettings, 1)
    settings.testing_enabled, settings.testing_user_id = True, account.id
    unit.db.commit()

    found = Material()
    project = unit.project("ZZPRUEBAPROY")
    volume = unit.volume(project_id=project["id"], name="ZZPRUEBATOMO")
    sub = unit.volume(parent_id=volume["id"], name="ZZPRUEBASUB")
    found.ids["project"].add(project["id"])
    found.ids["collection"].update({volume["id"], sub["id"]})
    captured = []
    for body, path in (({"project_name": "ZZPRUEBAPROY", "collection_id": volume["id"]}, "/cameras/capture/dual"),
                       ({"project_name": "ZZPRUEBAPROY", "collection_id": sub["id"], "camera_index": 0},
                        "/cameras/capture")):
        resp = unit.post("testing", path, body)
        assert resp.status_code == 200 and resp.json()["success"], resp.text
        captured.append(resp.json()["record_id"])
    found.ids["record"].update(captured)
    dual, single = captured
    resp = unit.post("testing", "/records/", {"title": "ZZPRUEBAHOJA", "capture_mode": "single",
                                              "collection_id": volume["id"]})
    assert resp.status_code == 200, resp.text
    found.ids["record"].add(resp.json()["id"])
    resp = unit.client.post(f"/records/{single}/images", headers=unit.headers["testing"],
                            files={"file": ("zzpruebaarchivo.jpg", b"\xff\xd8 zzprueba upload", "image/jpeg")})
    assert resp.status_code == 200, resp.text
    resp = unit.post("testing", f"/records/{dual}/missing-page", {"side": "left"})
    assert resp.status_code == 200, resp.text
    unit.db.expire_all()
    found.ids["image"].update(i for (i,) in unit.db.query(RecordImage.id)
                              .filter(RecordImage.record_id.in_(found.ids["record"])).all())
    found.volume_id, found.sub_id = volume["id"], sub["id"]
    return found


@pytest.fixture
def unit(client, db_session, override_projects_root, backend):  # noqa: F811
    _unit(db_session, "offline", True)
    u = Unit(db_session, client, override_projects_root, backend)
    u.real_volume.external_id = REAL_STEM
    db_session.commit()
    return u


# ---------------------------------------------------------------------------
# What leaves, and what is queued to leave
# ---------------------------------------------------------------------------

def _has_needle(text) -> bool:
    lowered = str(text).lower()
    return any(n in lowered for n in NEEDLES)


_ID_KEY = {"project": ("project",), "collection": ("collection", "volume", "parent"),
           "record": ("record", "opening"), "image": ("image",)}


def _ids_with_context(value, material, where, found):
    """Each (where, key, id) in a JSON value whose key names a kind and whose id is that kind's testing id."""
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if "id" in lowered:
                for kind, words in _ID_KEY.items():
                    if any(w in lowered for w in words):
                        ids = item if isinstance(item, list) else [item]
                        for i in ids:
                            if isinstance(i, int) and not isinstance(i, bool) and i in material.ids[kind]:
                                found.append((where, key, i))
            _ids_with_context(item, material, where, found)
    elif isinstance(value, list):
        for item in value:
            _ids_with_context(item, material, where, found)


_URL_ID = re.compile(r"/(projects|collections|volumes|records|images)/(\d+)(?:/|$|\?)")
_URL_KIND = {"projects": "project", "collections": "collection", "volumes": "collection",
             "records": "record", "images": "image"}


def _queued_rows(db):
    """Every row of every table that holds work queued to leave the unit: the outbox's and the offsite tables."""
    from sqlalchemy import select
    from app.core.db import Base
    rows = {}
    for name, table in Base.metadata.tables.items():
        if name.startswith("fisqua") or name.startswith("offsite"):
            rows[name] = [dict(r._mapping) for r in db.execute(select(table)).all()]
    return rows


def _data_files(data_dir: Path):
    return {str(p.relative_to(data_dir)): p.read_bytes() for p in sorted(data_dir.rglob("*")) if p.is_file()}


def _run_every_sender(unit, wire, remote, mode, material):  # noqa: F811
    """Run each sender once in `mode`; answers the catalogue dump's argv."""
    from app.core import catalogue_backup, device_id, fisqua_outbox, fisqua_pairing, fisqua_queue
    from app.core import image_upload, offsite_free
    db = unit.db

    # The offsite image upload.
    upload = image_upload.run_image_upload(db)
    assert upload["status"] == "done", upload

    # The handover: the real volume's, and the testing volume's refused to every caller.
    resp = unit.post("operator", f"/collections/{unit.real_volume.id}/promote")
    assert resp.status_code == 200, resp.text
    assert unit.post("testing", f"/collections/{material.volume_id}/promote").status_code == 403
    for caller in ("operator", "admin"):
        assert unit.post(caller, f"/collections/{material.volume_id}/promote").status_code == 404

    # The outbox, the status poll and the decisions pull.
    fisqua_outbox.run_fisqua_outbox(db)
    fisqua_outbox.poll_fisqua_status(db)
    # The operators list, then the queue and its acknowledgement.
    fisqua_queue.send_operators(db)
    fisqua_queue.sync_fisqua_queue(db)

    # The reference code lookup (an admin's real volume), and the testing account's volume, which looks nothing up.
    resp = unit.post("admin", "/collections/", {"name": "Otro tomo", "project_id": unit.real_project.id,
                                                "filename_stem": LOOKUP_CODE, "external_id": LOOKUP_CODE})
    assert resp.status_code == 201, resp.text
    before = len(wire.requests)
    project_id = next(iter(material.ids["project"]))
    resp = unit.post("testing", "/collections/", {"name": "ZZPRUEBAOTRO", "project_id": project_id})
    assert resp.status_code == 201, resp.text
    material.ids["collection"].add(resp.json()["id"])
    assert len(wire.requests) == before
    # The description search.
    resp = unit.get("admin", "/fisqua/descriptions", params={"q": "Tomo real"})
    assert resp.status_code in (200, 409, 503), resp.text

    # Pairing by a code and its renewal, with the client the registry builds.
    device_id.initialise()
    clock = [0.0]
    registry = fisqua_pairing.PairingRegistry(apply=lambda payload, session: None, start_threads=False,
                                              clock=lambda: clock[0])
    session = registry.begin("Sala 1")
    session.step()
    clock[0] += 10.0
    session.touch()
    session.step()
    assert session.state in fisqua_pairing.RENEWABLE, session.state
    renewed = registry.renew()
    renewed.step()

    # Freeing and restoring the testing volume's files: refused before anything is listed.
    for call in (lambda: offsite_free.plan_free(db, material.volume_id),
                 lambda: offsite_free.start_free(db, material.volume_id, "u-admin"),
                 lambda: offsite_free.restore_volume(db, material.volume_id, "u-admin")):
        with pytest.raises(Exception):
            call()
    db.rollback()

    # The catalogue backup: the exception, the full dump.
    return catalogue_backup.dump_argv()


@pytest.mark.unit
def test_the_queue_reads_a_kept_volume_by_real_volumes_only(unit):
    """A queue item naming a testing volume's stem finds no kept volume: the query reads real volumes only."""
    from app.core import fisqua_queue
    project = unit.project("ZZPRUEBAPROY")
    volume = unit.volume(project_id=project["id"])
    assert fisqua_queue._kept_volume_for(unit.db, None, volume["filename_stem"]) is False
    assert fisqua_queue._kept_volume_for(unit.db, volume["filename_stem"], None) is False
    # A real kept volume is still found by its stem.
    unit.real_volume.kept_on_unit = True
    unit.db.commit()
    assert fisqua_queue._kept_volume_for(unit.db, None, REAL_STEM) is True


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["backup", "fisqua"])
def test_nothing_of_the_testing_space_leaves_the_unit(unit, wire, paired, mode):
    from app.core import catalogue_backup, config
    remote = paired
    dump_before = catalogue_backup.dump_argv()
    material = _testing_material(unit)
    _unit(unit.db, mode)
    data_dir = Path(config.settings.DATA_DIR)

    dump_after = _run_every_sender(unit, wire, remote, mode, material)

    # The senders were reached: real work left, through every path the mode allows.
    bucket = remote.files()
    assert any(f"/{REAL_STEM}/captures/" in key for key in bucket), sorted(bucket)
    sent = [(r["method"], r["url"].split("?")[0]) for r in wire.requests]
    pairing = [r for r in wire.requests if "/pairing/" in r["url"]]
    assert len(pairing) == 3, sent
    if mode == "fisqua":
        for part in ("/manifests", "/volumes/status", "/decisions", "/operators", "/queue", "/queue/ack",
                     f"/descriptions/{LOOKUP_CODE}", "/descriptions?"):
            assert any(part in r["url"] for r in wire.requests), (part, sent)
    else:
        # Backup mode: nothing but pairing goes to Fisqua.
        assert len(wire.requests) == len(pairing), sent

    # No testing string anywhere that leaves or waits to leave.
    leaks = []
    for r in wire.requests:
        for part in ("url", "body"):
            if _has_needle(r[part]):
                leaks.append(("http", r["method"], r["url"], part))
        if _has_needle(json.dumps(r["headers"])):
            leaks.append(("http", r["method"], r["url"], "headers"))
    for key, data in bucket.items():
        if _has_needle(key) or _has_needle(data.decode("latin-1")):
            leaks.append(("bucket", key))
    for argv in remote.calls():
        if _has_needle(" ".join(argv)):
            leaks.append(("rclone", argv))
    queued = _queued_rows(unit.db)
    for table, rows in queued.items():
        for row in rows:
            if _has_needle(json.dumps(row, default=str)):
                leaks.append(("row", table, row))
    for name, data in _data_files(data_dir).items():
        if _has_needle(name) or _has_needle(data.decode("latin-1")):
            leaks.append(("data", name))
    assert leaks == []

    # No testing id named with its kind.
    with_context = []
    for r in wire.requests:
        for kind, value in _URL_ID.findall(r["url"]):
            if int(value) in material.ids[_URL_KIND[kind]]:
                with_context.append(("url", r["url"]))
        try:
            body = json.loads(r["body"]) if r["body"] else None
        except ValueError:
            body = None
        _ids_with_context(body, material, r["url"], with_context)
    for table, rows in queued.items():
        for row in rows:
            if row.get("collection_id") in material.ids["collection"]:
                with_context.append((table, "collection_id", row["collection_id"]))
            if row.get("record_image_id") in material.ids["image"]:
                with_context.append((table, "record_image_id", row["record_image_id"]))
    assert with_context == []

    # The catalogue backup, the one exception: the full dump, as on a unit with no testing material.
    assert dump_after == dump_before
    assert "pg_dumpall" in dump_after
    filters = ("--table", "--exclude-table", "--exclude-table-data", "--schema", "--exclude-schema",
               "--data-only", "--schema-only", "--database", "--exclude-database")
    assert not any(word.split("=", 1)[0] in filters for word in dump_after)
    assert set(SENDERS) >= {"image_upload", "fisqua_outbox", "status_poll", "decisions_pull", "operators",
                            "queue_sync", "pairing", "catalogue_backup"}
