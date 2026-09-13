"""A CHDK body that never delivers has to reach the operator as a timeout.

The test-capture route answers 504 for a stalled capture and 500 for
everything else, and it tells them apart by class: _is_capture_timeout walks
the exception chain looking for CaptureTimeoutError, which it imports from
the gphoto2 backend. That import is now a re-export of the shared class, so
this is the test that the CHDK backend's own timeout comes out the same door
- if the two ever became separate classes, a stalled compact would read as a
crashed one and the operator would be told the wrong thing.

The whole path runs for real: the route, the capture service, the backend,
and a scripted body whose remote capture never completes.
"""

import pytest

import capture.project_manager as project_manager_module
import capture.service as capture_service

from .chdk_fakes import Body, PTPError, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"


@pytest.fixture(autouse=True)
def isolated_projects_root(monkeypatch, tmp_path):
    """Keep the test capture's throwaway files away from any real root."""
    from app.core.config import settings

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    return root


def _user(username, role):
    from app.models.user import User

    return User(
        username=username,
        email=f"{username}@example.com",
        hashed_password="x",
        role=role,
        is_active=True,
    )


def _client_as(client, username, role):
    from app.main import app
    from app.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: _user(username, role)
    return client


def _install(monkeypatch, body):
    def fake_default_camera_config_from_registry(
        camera_index, resolution="high", registry=None
    ):
        return {"camera_index": camera_index}, "hw"

    monkeypatch.setattr(
        project_manager_module,
        "default_camera_config_from_registry",
        fake_default_camera_config_from_registry,
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    return backend


@pytest.mark.unit
def test_a_body_that_never_delivers_is_a_504(client, monkeypatch):
    body = Body(
        serial="AAA111", card=EVEN_CARD,
        shoot_error=TimeoutError("Remote capture did not complete"),
    )
    _install(monkeypatch, body)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 504, resp.text
    assert "usb:001,004" in resp.json()["detail"]


@pytest.mark.unit
def test_a_body_that_fails_over_ptp_is_a_500(client, monkeypatch):
    """Not every failure is a timeout; 0x2002 is a different conversation."""
    body = Body(serial="AAA111", card=EVEN_CARD, shoot_error=PTPError(0x2002))
    _install(monkeypatch, body)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 500, resp.text
    assert "PTP 0x2002" in resp.json()["detail"]


@pytest.mark.unit
def test_a_body_that_delivers_comes_back_as_a_jpeg(client, monkeypatch):
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=b"\xff\xd8\xff\xe0 page \xff\xd9",
    )
    _install(monkeypatch, body)
    api = _client_as(client, "op", "operator")

    resp = api.post("/cameras/test-capture/0")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == body.image
