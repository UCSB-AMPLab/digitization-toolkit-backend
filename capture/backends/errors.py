"""
Errors shared by every camera backend.

A failure that the API layer has to recognise by class - rather than by
reading a message - cannot live in one backend's module, or the same
situation on another backend travels as a different class and is handled
differently. CaptureTimeoutError is the case that matters today: a stalled
capture becomes a 504 (app/api/cameras.py:_is_capture_timeout), and a DSLR
that never delivers a file and a CHDK body that never delivers its bytes are
the same event to the operator.

Backends may re-export from here for the sake of existing imports, but must
not define their own copy.
"""


class CaptureTimeoutError(RuntimeError):
    """No image arrived from the camera before the capture deadline."""
