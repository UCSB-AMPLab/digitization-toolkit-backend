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


@pytest.mark.unit
def test_cleanup_leaves_nothing_open_when_a_body_arrives_as_it_runs(monkeypatch):
    """A body opened after cleanup was asked for still has to be closed.

    Cleanup used to take one snapshot of the map and close what was in it, so
    an enumeration that opened a newly arrived body in the meantime left that
    body mapped with a live claim while cleanup reported every camera closed.
    Cleanup now runs under the same lock as an enumeration and drains the map
    rather than a copy of it, so the two cannot overlap and nothing can be
    open when it returns.

    The enumeration is paused inside the bus scan, where it holds that lock,
    so cleanup provably cannot be running alongside it rather than merely
    not being observed to.
    """
    staying = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    arriving = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD)
    fake = make_pychdk(staying)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()

    fake.bodies = [staying, arriving]
    scanning = fake.gate_list()
    scan = _run(backend.list_devices)
    scanning.wait_until_entered()

    done = threading.Event()
    cleaner = _run(lambda: (backend.cleanup(), done.set()))
    cleaner.join(timeout=1)
    assert not done.is_set(), "cleanup ran while an enumeration was in flight"

    scanning.release()
    scan.join(timeout=10)
    cleaner.join(timeout=10)

    assert done.is_set(), "cleanup never finished"
    assert arriving.opens == 1, "the enumeration never opened the new body"
    assert (staying.live, arriving.live) == (0, 0), "a camera was left open"
    assert backend._bodies == {}, "a body was left mapped after cleanup"


@pytest.mark.unit
def test_the_backend_looks_at_the_bus_again_after_a_cleanup(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()
    backend.cleanup()

    assert backend.is_camera_connected(0) is True
    assert body.opens == 2
