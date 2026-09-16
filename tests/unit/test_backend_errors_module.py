"""A capture deadline belongs to every backend, not to gphoto2 alone.

A CHDK body stops answering exactly the way a DSLR does, and the route that
turns a stalled capture into a 504 (app/api/cameras.py:_is_capture_timeout)
recognises one class only. So CaptureTimeoutError lives in
capture/backends/errors.py, where any backend can raise it, and
gphoto2_backend re-exports the same object rather than defining a second
class: two classes with one name would make the 504 depend on which backend
was running.
"""

import pytest


@pytest.mark.unit
def test_the_timeout_error_is_defined_in_the_shared_errors_module():
    from capture.backends.errors import CaptureTimeoutError

    assert CaptureTimeoutError.__module__ == "capture.backends.errors"
    assert issubclass(CaptureTimeoutError, RuntimeError)


@pytest.mark.unit
def test_the_gphoto2_name_is_the_same_object_not_a_second_class():
    from capture.backends.errors import CaptureTimeoutError as shared
    from capture.backends.gphoto2_backend import CaptureTimeoutError as legacy

    assert legacy is shared


@pytest.mark.unit
def test_a_timeout_raised_anywhere_is_caught_by_the_gphoto2_import():
    """The route imports the name from gphoto2_backend; a CHDK timeout must match."""
    from capture.backends.errors import CaptureTimeoutError as shared
    from capture.backends.gphoto2_backend import CaptureTimeoutError as legacy

    with pytest.raises(legacy):
        raise shared("no bytes after 30.0s")
