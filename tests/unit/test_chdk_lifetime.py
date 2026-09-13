"""Who may close a body, and when a second one may be opened for its port.

A camera can be claimed once. Everything that opens or closes one therefore
has to be serialised against everything that uses one, or the appliance ends
up with two PTP claims on a body that can only answer one, or with a device
closed under a capture that is still running.

Two rules hold that together: a body is closed while its own lock is held,
and it is dropped from the map only after it has been closed, so there is
never a moment in which its port looks free while its device is still open.

These tests stop one thread inside a call and run another past it, so they
are about the windows, not the happy path.
"""

import threading

import pytest

from capture.camera import CameraConfig

from .chdk_fakes import Body, PTPError, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"


def _run(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


@pytest.mark.unit
def test_no_second_device_is_opened_while_the_first_is_closing(monkeypatch, tmp_path):
    """Evicting a body must not free its port before the device is closed."""
    body = Body(serial="AAA111", card=EVEN_CARD, shoot_error=PTPError(0x2002))
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    closing = body.gate("close")
    failures = []

    def capture():
        try:
            backend.capture_image(
                tmp_path / "page.jpg", CameraConfig(camera_index=0)
            )
        except RuntimeError:
            pass
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    worker = _run(capture)
    closing.wait_until_entered()

    # The body is mid-close. An enumeration now must not hand out a second
    # claim on the same camera.
    scan = _run(backend.list_devices)
    scan.join(timeout=1)
    closing.release()
    worker.join(timeout=10)
    scan.join(timeout=10)

    assert not failures, failures
    assert body.max_live == 1, (
        f"two devices were open at once for one camera ({body.max_live})"
    )


@pytest.mark.unit
def test_cleanup_waits_for_a_capture_that_is_still_running(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    shooting = body.gate("shoot")
    done = threading.Event()

    def capture():
        backend.capture_image(tmp_path / "page.jpg", CameraConfig(camera_index=0))

    worker = _run(capture)
    shooting.wait_until_entered()

    cleaner = _run(lambda: (backend.cleanup(), done.set()))
    cleaner.join(timeout=1)
    assert not done.is_set(), "cleanup closed a camera out from under a capture"
    assert body.closes == 0

    shooting.release()
    worker.join(timeout=10)
    cleaner.join(timeout=10)

    assert done.is_set(), "cleanup never finished"
    assert body.closes == 1
    assert (tmp_path / "page.jpg").read_bytes() == JPEG


@pytest.mark.unit
def test_an_enumeration_waits_for_a_capture_on_a_body_that_has_left(
    monkeypatch, tmp_path
):
    """A body that leaves the bus mid-capture is closed after the capture, not during."""
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()

    shooting = body.gate("shoot")

    worker = _run(
        lambda: backend.capture_image(
            tmp_path / "page.jpg", CameraConfig(camera_index=0)
        )
    )
    shooting.wait_until_entered()

    fake.bodies = []
    scanned = threading.Event()
    scan = _run(lambda: (backend.list_devices(), scanned.set()))
    scan.join(timeout=1)
    assert not scanned.is_set(), "the enumeration closed a body mid-capture"

    shooting.release()
    worker.join(timeout=10)
    scan.join(timeout=10)

    assert scanned.is_set()
    assert body.closes == 1
    assert (tmp_path / "page.jpg").read_bytes() == JPEG
