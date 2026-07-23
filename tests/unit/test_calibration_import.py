"""Regression test for NEH-113: capture/calibration.py must not hard-import
picamera2 at module load time. On non-Linux dev machines (macOS) the bare
`from picamera2 import Picamera2` blew up import of the whole module; it
must now guard the import the same way capture/backends/picamera2_backend.py
does, and raise a clear RuntimeError if calibration is attempted without it.
"""

import pytest


def test_module_imports_and_constructs_without_picamera2():
    import capture.calibration

    cal = capture.calibration.CameraCalibration(camera_index=0)
    assert cal.camera_index == 0


@pytest.mark.unit
def test_calibrate_focus_raises_when_picamera2_unavailable(monkeypatch):
    import capture.calibration as calibration

    monkeypatch.setattr(calibration, "_PICAMERA2_AVAILABLE", False)

    cal = calibration.CameraCalibration(camera_index=0)
    with pytest.raises(RuntimeError, match="picamera2 is not available"):
        cal.calibrate_focus()
