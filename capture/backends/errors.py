"""
Errors shared by every camera backend.

A failure that the API layer has to recognise by class - rather than by
reading a message - cannot live in one backend's module, or the same
situation on another backend travels as a different class and is handled
differently. CaptureTimeoutError is the case that matters today: a stalled
capture becomes a 504 (app/api/cameras.py:_is_capture_timeout), and a DSLR
that never delivers a file and a CHDK body that never delivers its bytes are
the same event to the operator. The preview route
(app/api/cameras.py:get_camera_preview) answers 503 for PreviewFrameFailedError
and 404 for CameraNotConnectedError and every other RuntimeError.

Backends may re-export from here for the sake of existing imports, but must
not define their own copy.

Version: 0.3.0
"""


class CaptureTimeoutError(RuntimeError):
    """No image arrived from the camera before the capture deadline."""


class CameraNotConnectedError(RuntimeError):
    """No body answers at this camera index, as of a look that completed."""


class PreviewFrameFailedError(RuntimeError):
    """A connected body with a live session did not deliver this preview frame.

    Raised only where the backend knows the body is there and only the frame
    failed, so the next poll may succeed.
    """
