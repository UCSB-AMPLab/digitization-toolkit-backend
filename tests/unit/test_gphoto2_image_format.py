"""The gphoto2 backend's capture formats: JPEG and RAW+JPEG, with RAW as the old name.

RAW+JPEG means a raw master with a JPEG preview. On a T7 that is the
camera's imageformat "RAW": the camera writes one .cr2 and the backend
extracts the preview from it. "RAW + L" would make the camera write two
files, and the backend collects one, so it is never set. A camera that
reports "RAW" or "RAW + L" is reported as RAW+JPEG.

No camera is involved: the session is a stand-in that records what is set.

Run with: python -m pytest tests/unit/test_gphoto2_image_format.py
"""

import logging
from types import SimpleNamespace

import pytest

import capture.backends.gphoto2_backend as gb


class _Session:
    """Records the PTP widgets set and answers get_info from them."""

    def __init__(self, imageformat="L"):
        self.widgets = {"imageformat": imageformat}
        self.set_calls = []

    def _set_config(self, key, value):
        self.set_calls.append((key, value))
        self.widgets[key] = value
        return True

    def _enforce_flash_off(self):
        return True

    def get_info(self):
        return dict(self.widgets)


def _backend(monkeypatch, session):
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "gp", SimpleNamespace(GPhoto2Error=RuntimeError))
    backend = gb.GPhoto2Backend(logging.getLogger("test-gphoto2-image-format"))
    monkeypatch.setattr(backend, "_get_or_open_session", lambda index: session)
    return backend


@pytest.mark.unit
@pytest.mark.parametrize("image_format", ["RAW+JPEG", "RAW"])
def test_raw_plus_jpeg_or_its_old_name_sets_the_cameras_raw(image_format):
    session = _Session()

    gb._PTPSession.apply_dslr_config(session, SimpleNamespace(image_format=image_format))

    assert ("imageformat", "RAW") in session.set_calls
    assert ("imageformat", "RAW + L") not in session.set_calls


@pytest.mark.unit
@pytest.mark.parametrize("image_format", ["RAW+JPEG", "RAW"])
def test_the_settings_route_sets_raw_and_reports_raw_plus_jpeg(monkeypatch, image_format):
    session = _Session()
    backend = _backend(monkeypatch, session)

    updated = backend.apply_dslr_settings(0, {"image_format": image_format})

    assert session.set_calls == [("imageformat", "RAW")]
    assert updated["image_format"] == "RAW+JPEG"


@pytest.mark.unit
@pytest.mark.parametrize("widget, reported", [
    ("L", "JPEG"),
    ("RAW", "RAW+JPEG"),
    ("RAW + L", "RAW+JPEG"),
])
def test_the_cameras_own_format_is_reported_by_its_current_name(
    monkeypatch, widget, reported
):
    backend = _backend(monkeypatch, _Session(imageformat=widget))

    assert backend.get_dslr_settings(0)["image_format"] == reported


@pytest.mark.unit
def test_capabilities_offer_jpeg_and_raw_plus_jpeg(monkeypatch):
    backend = _backend(monkeypatch, _Session())

    caps = backend.get_capabilities()

    assert caps["image_formats"] == ["JPEG", "RAW+JPEG"]
    assert caps["dslr_settings"] is True


@pytest.mark.unit
def test_the_settings_route_takes_raw_for_a_dslr_and_refuses_an_unknown_format(
    client, monkeypatch
):
    import capture.service as capture_service
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    session = _Session()
    backend = _backend(monkeypatch, session)
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x",
        role="operator", is_active=True,
    )

    ok = client.put("/cameras/dslr/0/settings", json={"image_format": "RAW"})
    refused = client.put("/cameras/dslr/0/settings", json={"image_format": "RAW + L"})

    assert ok.status_code == 200, ok.text
    assert ok.json()["image_format"] == "RAW+JPEG"
    assert session.set_calls == [("imageformat", "RAW")]
    assert refused.status_code == 422
    assert "JPEG, RAW+JPEG" in refused.json()["detail"]
