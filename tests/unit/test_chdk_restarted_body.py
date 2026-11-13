"""A camera restarted with its USB cable connected comes back at a new address.

On hardware the body left the bus and returned as usb:001,017 after being
usb:001,016. The first request after that failed with errno 19 (ENODEV) on the
old session. The backend drops that body, rescans once, and runs the request
once more on the body now at that index, provided that body has the same
hardware identity. A different or missing identity is refused, and only one
retry is made.

Version: 0.1.0-beta
"""

import pytest

from capture.camera import CameraConfig

from .chdk_fakes import Body, make_backend, make_pychdk, viewport_frame


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
JPEG = b"\xff\xd8\xff\xe0 a page \xff\xd9"


def _gone():
    return OSError(19, "No such device (it may have been disconnected)")


def _config():
    return CameraConfig(camera_index=0)


def _setup(monkeypatch, old, new):
    fake = make_pychdk(old)
    backend = make_backend(monkeypatch, fake)
    backend.list_devices()
    # The camera restarts: the old address leaves the bus, a new one appears.
    fake.bodies = [new]
    return backend, fake


def _old(**kwargs):
    return Body(address=16, serial="AAA111", card=EVEN_CARD, **kwargs)


def _new(**kwargs):
    return Body(address=17, serial="AAA111", card=EVEN_CARD, **kwargs)


@pytest.mark.unit
def test_a_preview_on_a_restarted_body_is_retried_on_the_new_address(monkeypatch):
    old = _old(frame=viewport_frame(), preview_error=_gone())
    new = _new(frame=viewport_frame())
    backend, fake = _setup(monkeypatch, old, new)
    scans = fake.list_calls

    jpeg = backend.capture_preview(0)

    assert jpeg[:2] == b"\xff\xd8"
    assert old.closes == 1
    assert fake.list_calls == scans + 1, "exactly one rescan"
    assert new.frames_served == 1
    assert backend._body_at(0).port.endswith("001,017")


@pytest.mark.unit
def test_a_capture_on_a_restarted_body_is_retried_on_the_new_address(
    monkeypatch, tmp_path
):
    old = _old(image=JPEG, shoot_error=_gone())
    new = _new(image=JPEG)
    backend, fake = _setup(monkeypatch, old, new)

    path, _ = backend.capture_image(tmp_path / "page.jpg", _config())

    assert (tmp_path / "page.jpg").read_bytes() == JPEG
    assert len(old.shots) == 1 and len(new.shots) == 1


@pytest.mark.unit
def test_a_different_identity_at_the_index_is_refused(monkeypatch, tmp_path):
    old = _old(image=JPEG, shoot_error=_gone())
    new = Body(address=17, serial="BBB222", card=EVEN_CARD, image=JPEG)
    backend, fake = _setup(monkeypatch, old, new)
    scans = fake.list_calls

    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert fake.list_calls == scans + 1, "the guard, not a missing rescan, refused"
    assert new.shots == []


@pytest.mark.unit
def test_a_body_with_no_identity_is_not_retried(monkeypatch):
    old = Body(address=16, serial=None, card=None, frame=viewport_frame(),
               preview_error=_gone())
    new = Body(address=17, serial=None, card=None, frame=viewport_frame())
    backend, fake = _setup(monkeypatch, old, new)
    scans = fake.list_calls

    with pytest.raises(RuntimeError):
        backend.capture_preview(0)

    assert new.frames_served == 0
    assert fake.list_calls == scans, "no rescan at all"


@pytest.mark.unit
def test_a_failure_that_is_not_a_gone_device_is_not_retried(monkeypatch):
    old = _old(frame=viewport_frame(), preview_error=OSError(5, "Input/output error"))
    new = _new(frame=viewport_frame())
    backend, fake = _setup(monkeypatch, old, new)
    scans = fake.list_calls

    with pytest.raises(RuntimeError):
        backend.capture_preview(0)

    assert new.frames_served == 0
    assert fake.list_calls == scans


@pytest.mark.unit
def test_only_one_retry_is_made(monkeypatch):
    old = _old(frame=viewport_frame(), preview_error=_gone())
    new = _new(frame=viewport_frame(), preview_error=_gone())
    backend, fake = _setup(monkeypatch, old, new)
    scans = fake.list_calls

    with pytest.raises(RuntimeError):
        backend.capture_preview(0)

    assert fake.list_calls == scans + 1
    assert new.closes == 1


@pytest.mark.unit
def test_a_layout_published_after_the_guard_is_refused_on_the_locked_body(
    monkeypatch, tmp_path
):
    old = _old(image=JPEG, shoot_error=_gone())
    new = _new(image=JPEG)
    other = Body(address=18, serial="BBB222", card=b"ODD\nid=bbbbbbbbbbbb\n",
                 image=JPEG)
    backend, fake = _setup(monkeypatch, old, new)
    fake.bodies = [new, other]

    real_rescan = backend.rescan
    real_body_at = backend._body_at
    state = {"rescanned": False, "calls": 0, "other": None}

    def rescan():
        rows = real_rescan()
        state["other"] = real_body_at(1)
        state["rescanned"] = True
        return rows

    def body_at(index):
        if state["rescanned"] and index == 0:
            state["calls"] += 1
            # The first lookup after the rescan is the guard's. Every later
            # one sees a layout published in between.
            if state["calls"] > 1:
                return state["other"]
        return real_body_at(index)

    monkeypatch.setattr(backend, "rescan", rescan)
    monkeypatch.setattr(backend, "_body_at", body_at)

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "001,016" in str(exc.value), "the first error is the one raised"
    assert other.shots == []
    assert new.shots == []


@pytest.mark.unit
def test_a_retry_that_fails_as_gone_again_raises_the_first_error(
    monkeypatch, tmp_path
):
    old = _old(image=JPEG, shoot_error=_gone())
    new = _new(image=JPEG, shoot_error=_gone())
    backend, _ = _setup(monkeypatch, old, new)

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "001,016" in str(exc.value)
    assert "001,017" not in str(exc.value)


@pytest.mark.unit
def test_any_other_failure_of_the_retry_propagates_as_itself(
    monkeypatch, tmp_path
):
    old = _old(image=JPEG, shoot_error=_gone())
    new = _new(image=JPEG, shoot_error=OSError(5, "Input/output error"))
    backend, _ = _setup(monkeypatch, old, new)

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "001,017" in str(exc.value)
    assert "Input/output error" in str(exc.value)
