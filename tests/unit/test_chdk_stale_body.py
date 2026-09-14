"""A queued operation must not run on the body that replaced the one it wanted.

Resolving a camera index to a body and acquiring that body's lock are two
different moments, and a rescan or a side assignment in between moves bodies
from index to index. A capture that decided on index 0 before the wait and
shot whatever held index 0 after it would file the left page as the right one
- silently, and in a way nobody notices until the volume is online.

So the index is resolved again once the lock is held, and the refusal rules
are read again with it. A body that moved, went away, or became unusable
while the operation queued is a failure, not something to shoot anyway.

Each test here parks the queued operation before the lock, runs the rescan to
completion, and only then lets it through, so the layout it revalidates
against has certainly been published. Pausing the rescan instead does not
establish that: it releases each body's lock as soon as it has read that
body's card, long before it publishes, so the queued operation can wake and
correctly succeed against a layout nothing has changed yet - a test built
that way passes or fails on scheduling, which is the one thing a proof of a
race must not do.
"""

import time

import pytest

from capture.camera import CameraConfig

from .chdk_fakes import (
    Body,
    Gate,
    make_backend,
    make_pychdk,
    park_at_lock,
    parse_own_txt,
    viewport_frame,
    watch_lock,
)


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"


def _queue(work, held):
    """Run `work` in a thread and wait until it has parked at the lock."""
    import threading

    result = {}

    def run():
        try:
            result["outcome"] = work()
        except RuntimeError as exc:
            result["outcome"] = str(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert held.arrived.wait(10), "the operation never reached the lock"
    return thread, result


@pytest.mark.unit
def test_a_capture_refuses_a_body_that_moved_while_it_waited(monkeypatch, tmp_path):
    """The capture wanted index 0; by the time it ran, that was another body."""
    one = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD, image=JPEG)
    two = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(one, two))
    backend.list_devices()
    held = park_at_lock(backend, 0)

    thread, result = _queue(
        lambda: backend.capture_image(
            tmp_path / "page.jpg", CameraConfig(camera_index=0)
        ) and "shot",
        held,
    )

    # The parities are swapped and the new layout is published in full while
    # the capture is parked: index 0 is the other body before it wakes.
    one.card = ODD_CARD
    two.card = EVEN_CARD
    rows = {row["index"]: row["serial"] for row in backend.rescan()}
    assert rows[0] == "BBB222", "the rescan did not move the bodies"

    held.let_through()
    thread.join(timeout=10)

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
    held = park_at_lock(backend, 0)

    thread, result = _queue(
        lambda: backend.capture_image(
            tmp_path / "page.jpg", CameraConfig(camera_index=0)
        ) and "shot",
        held,
    )

    twin = Body(bus=1, address=7, serial="BBB222", card=EVEN_CARD, image=JPEG)
    fake.bodies = [one, twin]
    assert all(row["error"] for row in backend.rescan()), "the twin was not seen"

    held.let_through()
    thread.join(timeout=10)

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
    held = park_at_lock(backend, 0)

    thread, result = _queue(lambda: backend.capture_preview(0) and "framed", held)

    one.card = ODD_CARD
    two.card = EVEN_CARD
    rows = {row["index"]: row["serial"] for row in backend.rescan()}
    assert rows[0] == "BBB222", "the rescan did not move the bodies"

    held.let_through()
    thread.join(timeout=10)

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
    held = park_at_lock(backend, 0)

    thread, result = _queue(lambda: backend.capture_preview(0)[:2], held)

    rows = {row["index"]: row["serial"] for row in backend.rescan()}
    assert rows[0] == "AAA111", "the rescan moved a body it should not have"

    held.let_through()
    thread.join(timeout=10)

    assert result.get("outcome") == b"\xff\xd8", result
    assert one.frames_served == 1


def _thread(target):
    import threading

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


@pytest.mark.unit
def test_no_layout_is_published_while_a_capture_is_running(monkeypatch, tmp_path):
    """Revalidating before the shutter is not enough on its own.

    A capture holds one body, and publishing a layout moves bodies between
    indices. Checking the index at the start and then running for seconds
    against a map anything may replace leaves the same wrong-camera failure
    one level out: the picture is taken correctly and filed under an index
    its body no longer has, with nothing failing and nothing to show for it
    in the manifest.
    """
    one = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD, image=JPEG)
    two = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(one, two))
    backend.list_devices()

    # The parities are swapped, so publishing would move the first body from
    # index 0 to index 1.
    one.card = ODD_CARD
    two.card = EVEN_CARD

    # Park the rescan after it has finished with the first body, so that
    # body's lock is free and a capture can start on it.
    reading = two.gate("download")
    rescan = _thread(backend.rescan)
    reading.wait_until_entered()

    shooting = one.gate("shoot")
    shooter = _thread(
        lambda: backend.capture_image(
            tmp_path / "page.jpg", CameraConfig(camera_index=0)
        )
    )
    shooting.wait_until_entered()

    reading.release()
    rescan.join(timeout=1)

    assert backend._body_at(0).serial == "AAA111", (
        "the layout moved under a running capture"
    )

    shooting.release()
    shooter.join(timeout=10)
    rescan.join(timeout=10)

    assert backend._body_at(1).serial == "AAA111", "the rescan never published"
    assert (tmp_path / "page.jpg").read_bytes() == JPEG
    assert len(one.shots) == 1


@pytest.mark.unit
def test_a_capture_cannot_run_inside_half_a_card_read(monkeypatch, tmp_path):
    """Reading a card is a download and then publishing what it said.

    A capture is admitted by _row_error, which reads the body's hardware id,
    and that id comes off the card when pyusb reads no USB serial. A capture
    that started between the download and the publish was therefore admitted
    against the card the rescan was replacing, and went on to write a page
    for whatever the card said afterwards. Here the rewritten card has lost
    its id line, so the body has no identity to record and may not capture at
    all: the outcome to assert is that no page is written, not that some
    check ran.
    """
    body = Body(serial=None, card=EVEN_CARD, image=JPEG)
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)
    assert backend.list_devices()[0]["error"] is None, "the body cannot capture"

    # The card is rewritten - by hand, or by a card reader - with its id line
    # gone, which leaves the body provisional and refused for capture.
    body.card = b"EVEN\n"

    # Park the rescan after the download and before it publishes what it read.
    parsing = Gate("parse_own_txt")

    def gated_parse(raw):
        parsing.arrive()
        return parse_own_txt(raw)

    fake.parse_own_txt = gated_parse
    watched = watch_lock(backend, 0)
    rescan = _thread(backend.rescan)
    parsing.wait_until_entered()

    shooting = body.gate("shoot")
    shot = {}

    def capture():
        try:
            shot["outcome"] = backend.capture_image(
                tmp_path / "page.jpg", CameraConfig(camera_index=0)
            )
        except RuntimeError as exc:
            shot["outcome"] = str(exc)

    shooter = _thread(capture)
    try:
        # The capture has arrived when one of two things is true: it reached
        # the shutter, because nothing held it back, or it had to wait for
        # the body's lock, because the read is holding it. Waiting for
        # whichever happens costs no fixed pause and leaves nothing to
        # scheduling.
        deadline = time.monotonic() + 10
        while not (shooting.entered.is_set() or watched.blocked.is_set()):
            assert time.monotonic() < deadline, "the capture never reached it"
            time.sleep(0.005)
    finally:
        # Both gates open before either thread is waited on, and in a finally
        # so that a failure above does not leave a daemon thread parked for
        # its whole timeout and report the failure from inside it. The
        # shutter gate opens before the rescan is joined for the same reason
        # in reverse: a capture that got through holds the body, the rescan
        # needs it back to publish, and joining the rescan first would block
        # for the timeout and fail on the wait rather than on the outcome.
        parsing.release()
        shooting.release()
        shooter.join(timeout=10)
        rescan.join(timeout=10)

    assert not shooter.is_alive() and not rescan.is_alive(), "a thread is stuck"
    assert not (tmp_path / "page.jpg").exists(), (
        "a page was filed for a body whose card gives it no identity"
    )
    assert "/side/" in str(shot.get("outcome")), shot
    assert body.shots == [], "the shutter fired inside the card read"
