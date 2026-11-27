"""The offsite settings routes (NEH-341): GET/POST /system/offsite, GET /system/offsite/template, POST /system/offsite/import.

The listing and the script are the fakes of test_offsite_settings; every
response body is kept and scanned for the application key.

    python -m pytest tests/unit/test_system_offsite.py

Version: 0.4.0
"""

import pytest

from tests.unit.test_capture_manifest_export import _as
from tests.unit.test_offsite_settings import (APP_KEY, BUCKET, KEY_ID, RECIPIENT, TENANT, file_text,  # noqa: F401
                                              script, unit, wired)


class Bodies:
    def __init__(self):
        self.seen = []

    def keep(self, response):
        self.seen.append(response.text)
        return response

    def assert_clean(self):
        assert APP_KEY not in "\n".join(self.seen)


@pytest.fixture
def bodies():
    kept = Bodies()
    yield kept
    kept.assert_clean()


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    """The mounts base, with one drive mounted directly under it; the mount itself is stood in for."""
    from app.api import system
    from app.core import offsite_settings

    base = tmp_path / "mounts"
    (base / "STICK").mkdir(parents=True)
    monkeypatch.setattr(system, "_MOUNT_BASE", base)
    monkeypatch.setattr(offsite_settings, "_mounted", lambda fd, base_fd: True)
    return base


def body(**over):
    out = {"bucketName": BUCKET, "applicationKeyId": KEY_ID, "applicationKey": APP_KEY, "tenant": TENANT,
           "cataloguePublicKey": RECIPIENT}
    out.update(over)
    return out


ROUTES = [("GET", "/system/offsite", None), ("GET", "/system/offsite/template", None),
          ("POST", "/system/offsite", body()), ("POST", "/system/offsite/import", {"mountpoint": "/x"})]


@pytest.mark.parametrize("role", ["operator", "reviewer"])
@pytest.mark.parametrize("method,path,json", ROUTES)
def test_administrators_only(client, wired, role, method, path, json):
    _as(client, role, role + "1")
    resp = client.request(method, path, json=json)
    assert resp.status_code == 403
    assert not wired.script.ran and wired.lister.calls == []


def test_the_template_is_plain_text_to_download(client, bodies):
    from app.core import offsite_settings

    resp = bodies.keep(_as(client, "admin", "admin1").get("/system/offsite/template"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["content-disposition"] == 'attachment; filename="captua-offsite.txt"'
    assert resp.text == offsite_settings.template_text()


def test_get_reports_the_settings_without_the_application_key(client, unit, monkeypatch, bodies):
    from app.core import config

    resp = bodies.keep(_as(client, "admin", "admin1").get("/system/offsite"))
    assert resp.json()["configured"] is False
    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    monkeypatch.setattr(config.settings, "OFFSITE_REMOTE", f"b2unit:{BUCKET}")
    monkeypatch.setattr(config.settings, "OFFSITE_DUMP_RECIPIENT", RECIPIENT)
    (unit / "data" / "rclone.conf").write_text(f"[b2unit]\ntype = b2\naccount = {KEY_ID}\nkey = {APP_KEY}\n")
    resp = bodies.keep(client.get("/system/offsite"))
    assert resp.status_code == 200
    assert resp.json() == {"configured": True, "bucketName": BUCKET, "tenant": TENANT, "applicationKeyId": KEY_ID,
                           "cataloguePublicKey": RECIPIENT, "restartRequired": False}


def test_the_form_saves(client, wired, bodies):
    resp = bodies.keep(_as(client, "admin", "admin1").post("/system/offsite", json=body()))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"outcome": "restarting", "stickFile": None}
    assert wired.script.calls[0].argv[1:3] == ["--tenant", TENANT]


def test_a_refused_value_names_the_key_only(client, wired, bodies):
    resp = bodies.keep(_as(client, "admin", "admin1").post("/system/offsite",
                                                           json=body(applicationKey="K000 FAKE")))
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "bad_value" and resp.json()["detail"]["key"] == "APPLICATION_KEY"
    assert "K000 FAKE" not in resp.text


@pytest.mark.parametrize("sent", [
    {k: v for k, v in body().items() if k != "tenant"},
    body(applicationKey=12345, extraField=APP_KEY),
    body(replace="yes"),
])
def test_a_validation_error_carries_loc_type_msg_only(client, wired, bodies, sent):
    resp = bodies.keep(_as(client, "admin", "admin1").post("/system/offsite", json=sent))
    assert resp.status_code == 422
    for error in resp.json()["detail"]:
        assert set(error) == {"loc", "type", "msg"}
    assert "12345" not in resp.text and "extraField" not in resp.text
    assert not wired.script.ran


def test_the_import_reads_the_stick_and_deletes_its_copy(client, wired, mounts, monkeypatch, bodies):
    from app.core import offsite_settings

    monkeypatch.setattr(offsite_settings, "_sync", lambda: None)
    path = mounts / "STICK" / "captua-offsite.txt"
    path.write_text(file_text())
    resp = bodies.keep(_as(client, "admin", "admin1").post("/system/offsite/import",
                                                           json={"mountpoint": str(mounts / "STICK")}))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"outcome": "restarting", "stickFile": "deleted"}
    assert not path.exists()


def test_an_import_refused_keeps_the_stick_copy(client, wired, mounts, bodies):
    path = mounts / "STICK" / "captua-offsite.txt"
    path.write_text(file_text())
    wired.lister.returncode = 1
    resp = bodies.keep(_as(client, "admin", "admin1").post("/system/offsite/import",
                                                           json={"mountpoint": str(mounts / "STICK")}))
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "listing_failed" and resp.json()["detail"]["stickFile"] == "kept"
    assert path.exists()


@pytest.mark.parametrize("where", ["outside", "dotdot", "symlink", "nested", "symlink_inside"])
def test_the_import_path_must_be_under_the_mounts_base(client, wired, mounts, tmp_path, where, bodies):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "captua-offsite.txt").write_text(file_text())
    if where == "outside":
        target = outside
    elif where == "dotdot":
        target = mounts / "STICK" / ".." / ".." / "outside"
    elif where == "symlink":
        (mounts / "LINK").symlink_to(outside)
        target = mounts / "LINK"
    elif where == "nested":
        (mounts / "STICK" / "sub").mkdir()
        (mounts / "STICK" / "sub" / "captua-offsite.txt").write_text(file_text())
        target = mounts / "STICK" / "sub"
    else:
        (mounts / "STICK" / "sub").mkdir()
        (mounts / "STICK" / "sub" / "captua-offsite.txt").write_text(file_text())
        (mounts / "STICK" / "jump").symlink_to(mounts / "STICK" / "sub")
        target = mounts / "STICK" / "jump"
    resp = bodies.keep(_as(client, "admin", "admin1").post("/system/offsite/import",
                                                           json={"mountpoint": str(target)}))
    assert resp.status_code == 400
    assert (outside / "captua-offsite.txt").exists() and not wired.script.ran
    if where in ("nested", "symlink_inside"):
        assert (mounts / "STICK" / "sub" / "captua-offsite.txt").exists()


def test_replace_is_passed_through_the_form(client, wired, monkeypatch, bodies):
    from app.core import config

    monkeypatch.setattr(config.settings, "OFFSITE_TENANT", TENANT)
    (wired.unit / "data" / "rclone.conf").write_text("[b2unit]\ntype = b2\naccount = 0000otherkeyid000\nkey = x\n")
    client = _as(client, "admin", "admin1")
    resp = bodies.keep(client.post("/system/offsite", json=body()))
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "replace_required"
    resp = bodies.keep(client.post("/system/offsite", json=body(replace=True)))
    assert resp.status_code == 200, resp.text
    assert wired.script.calls[0].argv[-1] == "--replace-credential"
