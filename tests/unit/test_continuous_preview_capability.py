"""NEH-334: backends report a ``continuous_preview`` capability.

``live_preview`` means a backend can return a preview frame on request.
``continuous_preview`` means the capture screen may poll preview frames
continuously. Only the picamera2 backend sets it.
"""

import pytest

import capture.service as capture_service
from capture.backends.base import CameraBackend
from capture.backends.chdk_backend import ChdkBackend
from capture.backends.gphoto2_backend import GPhoto2Backend
from capture.backends.picamera2_backend import Picamera2Backend
from capture.backends.subprocess_backend import RpicamBackend

pytestmark = pytest.mark.unit


# get_capabilities() reads no instance state, so it is called on the class.
EXPECTED = [
    (CameraBackend, False, False),
    (Picamera2Backend, True, True),
    (GPhoto2Backend, True, False),
    (ChdkBackend, True, False),
    (RpicamBackend, False, False),
]


@pytest.mark.parametrize("cls, live, continuous", EXPECTED)
def test_backend_reports_continuous_preview(cls, live, continuous):
    caps = cls.get_capabilities(object())

    assert caps["continuous_preview"] is continuous
    assert caps["live_preview"] is live


class _Picamera2LikeBackend:
    def get_capabilities(self):
        return Picamera2Backend.get_capabilities(object())

    def get_backend_name(self):
        return "picamera2"


def test_capabilities_route_includes_continuous_preview(client, monkeypatch):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x",
        role="operator", is_active=True,
    )
    monkeypatch.setattr(
        capture_service, "get_backend", lambda: _Picamera2LikeBackend()
    )

    resp = client.get("/cameras/capabilities")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "picamera2"
    assert body["continuous_preview"] is True
    assert body["live_preview"] is True
