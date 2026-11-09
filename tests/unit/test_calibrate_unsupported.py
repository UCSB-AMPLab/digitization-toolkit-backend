"""POST /cameras/calibrate and /cameras/calibrate/white-balance on a backend
that cannot calibrate answer 501, not 200 with success=false (NEH-326).

Both handlers raise HTTPException(501) inside a try whose generic
``except Exception`` used to catch it and return a 200 body, so a client
checking the status code saw success.
"""

import pytest

import capture.service as capture_service


class _NoCalibrationBackend:
    def __init__(self):
        self.calls = 0

    def get_capabilities(self):
        return {"autofocus_calibration": False}

    def get_backend_name(self):
        return "gphoto2"

    def run_autofocus_calibration(self, *a, **k):
        self.calls += 1
        raise AssertionError("an unsupported backend reached calibration")

    def run_white_balance_calibration(self, *a, **k):
        self.calls += 1
        raise AssertionError("an unsupported backend reached calibration")


def _as_operator(client):
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x",
        role="operator", is_active=True,
    )
    return client


@pytest.mark.unit
@pytest.mark.parametrize(
    "path, phrase",
    [
        ("/cameras/calibrate", "autofocus calibration"),
        ("/cameras/calibrate/white-balance", "white balance calibration"),
    ],
)
def test_unsupported_backend_gets_501(client, monkeypatch, path, phrase):
    backend = _NoCalibrationBackend()
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    api = _as_operator(client)

    resp = api.post(path, json={"camera_index": 0})

    assert resp.status_code == 501, resp.text
    detail = resp.json()["detail"]
    assert "gphoto2" in detail and phrase in detail
    assert backend.calls == 0
