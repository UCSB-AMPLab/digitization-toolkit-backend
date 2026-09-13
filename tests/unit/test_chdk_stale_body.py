"""A queued operation must not run on the body that replaced the one it wanted.

Resolving a camera index to a body and waiting for that body's lock are two
different moments, and a rescan or a side assignment in between moves bodies
from index to index. A capture that decided on index 0 before the wait and
shot whatever held index 0 after it would file the left page as the right one
- silently, and in a way nobody notices until the volume is online.

So the index is resolved again once the lock is held, and the refusal rules
are read again with it. A body that moved, went away, or became unusable
while the operation queued is a failure, not something to shoot anyway.

Holding that window open takes some care, because the two threads contend for
the same lock: a rescan cannot finish while a capture holds the body, and a
capture that wins the lock first is looking at a layout nothing has changed
yet. The order that matters, and the one reproduced here, is the other one -
the rescan holds the body while the capture arrives and blocks, and publishes
its new layout before the capture wakes.
"""

import threading

import pytest

from capture.camera import CameraConfig

from .chdk_fakes import (
    Body,
    make_backend,
    make_pychdk,
    viewport_frame,
    watch_lock,
)


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"


def _run(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _rescan_holding(backend, body):
    """Start a rescan and pause it inside its card read on `body`.

    It holds that body's lock until the returned gate is released, which is
    what lets an operation arrive and block on a body the rescan is about to
    move.
    """
    reading = body.gate("download")
    thread = _run(backend.rescan)
    reading.wait_until_entered()
    return reading, thread


@pytest.mark.unit
def test_a_capture_refuses_a_body_that_moved_while_it_waited(monkeypatch, tmp_path):
    """The capture wanted index 0; by the time it ran, that was another body."""
    one = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD, image=JPEG)
    two = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(one, two))
    backend.list_devices()
    watched = watch_lock(backend, 0)

    # The parities are swapped on the cards, and a rescan is under way.
    one.card = ODD_CARD
    two.card = EVEN_CARD
    reading, rescan = _rescan_holding(backend, one)

    result = {}

    def capture():
        try:
            backend.capture_image(
                tmp_path / "page.jpg", CameraConfig(camera_index=0)
            )
            result["outcome"] = "shot"
        except RuntimeError as exc:
            result["outcome"] = str(exc)

    queued = _run(capture)
    assert watched.waiting.wait(10), "the capture never reached the lock"

    reading.release()
    rescan.join(timeout=10)
    queued.join(timeout=10)

    assert result.get("outcome", "").startswith("Camera 0"), result
    assert "no longer" in result["outcome"], result
    assert one.shots == [], "the queued capture shot the body that moved"
    assert two.shots == [], "the queued capture shot the body that arrived"


@pytest.mark.unit
def test_a_capture_refuses_a_body_that_became_unusable_while_it_waited(
    monkeypatch, tmp_path
):
    """A second body arrives on the same parity while the capture queues."""
    one = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD, image=JPEG)
    fake = make_pychdk(one)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()
    watched = watch_lock(backend, 0)

    twin = Body(bus=1, address=7, serial="BBB222", card=EVEN_CARD, image=JPEG)
    fake.bodies = [one, twin]
    reading, rescan = _rescan_holding(backend, one)

    result = {}

    def capture():
        try:
            backend.capture_image(
                tmp_path / "page.jpg", CameraConfig(camera_index=0)
            )
            result["outcome"] = "shot"
        except RuntimeError as exc:
            result["outcome"] = str(exc)

    queued = _run(capture)
    assert watched.waiting.wait(10), "the capture never reached the lock"

    reading.release()
    rescan.join(timeout=10)
    queued.join(timeout=10)

    assert "even" in result.get("outcome", ""), result
    assert one.shots == [], "the queued capture shot a body that may not capture"


@pytest.mark.unit
def test_a_preview_refuses_a_body_that_moved_while_it_waited(monkeypatch):
    one = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD,
               frame=viewport_frame())
    two = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD,
               frame=viewport_frame())
    backend = make_backend(monkeypatch, make_pychdk(one, two))
    backend.list_devices()
    watched = watch_lock(backend, 0)

    one.card = ODD_CARD
    two.card = EVEN_CARD
    reading, rescan = _rescan_holding(backend, one)

    result = {}

    def preview():
        try:
            backend.capture_preview(0)
            result["outcome"] = "framed"
        except RuntimeError as exc:
            result["outcome"] = str(exc)

    queued = _run(preview)
    assert watched.waiting.wait(10), "the preview never reached the lock"

    reading.release()
    rescan.join(timeout=10)
    queued.join(timeout=10)

    assert "no longer" in result.get("outcome", ""), result
    assert one.frames_served == 0
    assert two.frames_served == 0


@pytest.mark.unit
def test_a_preview_still_runs_when_the_body_stayed_where_it_was(monkeypatch):
    """The revalidation must not refuse the ordinary case it was added for."""
    one = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD,
               frame=viewport_frame())
    two = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD,
               frame=viewport_frame())
    backend = make_backend(monkeypatch, make_pychdk(one, two))
    backend.list_devices()
    watched = watch_lock(backend, 0)

    reading, rescan = _rescan_holding(backend, one)

    result = {}

    def preview():
        try:
            result["outcome"] = backend.capture_preview(0)[:2]
        except RuntimeError as exc:  # pragma: no cover - surfaced by the assert
            result["outcome"] = str(exc)

    queued = _run(preview)
    assert watched.waiting.wait(10), "the preview never reached the lock"

    reading.release()
    rescan.join(timeout=10)
    queued.join(timeout=10)

    assert result.get("outcome") == b"\xff\xd8", result
