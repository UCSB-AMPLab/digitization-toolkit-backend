"""Busy responses keep capture ownership on the same session without control files."""
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import capture.backends.gphoto2_backend as gb
from tests.unit.test_gphoto2_capture_timeout import (
    _Clock, _Script, _make_backend, _file_path, _config, PORT,
    GP_EVENT_FILE_ADDED, GP_EVENT_TIMEOUT,
)
from tests.unit.test_gphoto2_recovery import recovery_state, _session


def _body(monkeypatch, exposed=True):
    clock = _Clock()
    script = _Script(clock=clock)
    backend, gp = _make_backend(monkeypatch, script)
    monkeypatch.setattr(gb.time, "monotonic", clock)
    pending, triggers, received = [], [], []

    def trigger(cam):
        script.trigger_calls += 1
        triggers.append((cam, clock.now))
        if script.trigger_calls == 1:
            if exposed:
                pending.append((clock.now + 3, "PAGE_N.JPG"))
            raise gp.GPhoto2Error("[-110] busy on release")
        name = "PAGE_N1.JPG" if exposed or script.trigger_calls > 2 else "PAGE_N.JPG"
        pending.append((clock.now + 3, name))

    def event(cam, ms):
        end = clock.now + ms / 1000
        if pending and pending[0][0] <= end:
            at, name = pending.pop(0)
            clock.now = at
            return GP_EVENT_FILE_ADDED, _file_path(name=name)
        clock.now = end
        return GP_EVENT_TIMEOUT, None

    def get(cam, folder, name, typ):
        received.append(name)
        return SimpleNamespace(save=lambda path: Path(path).write_bytes(name.encode()))

    monkeypatch.setattr(gp.Camera, "trigger_capture", trigger)
    monkeypatch.setattr(gp.Camera, "wait_for_event", event)
    monkeypatch.setattr(gp.Camera, "file_get", get)
    return backend, script, clock, pending, triggers, received


@pytest.mark.parametrize("focus_check", [False, True])
def test_busy_after_exposure_saves_this_page_then_the_next(monkeypatch, tmp_path, recovery_state, focus_check):
    backend, script, clock, pending, triggers, received = _body(monkeypatch)
    first, _ = backend.capture_image(tmp_path / "N.jpg", _config(), focus_check=focus_check)
    assert Path(first).read_bytes() == b"PAGE_N.JPG"
    second, _ = backend.capture_image(tmp_path / "N1.jpg", _config(), focus_check=focus_check)
    assert Path(second).read_bytes() == b"PAGE_N1.JPG"
    assert received == ["PAGE_N.JPG", "PAGE_N1.JPG"]
    assert [name for folder, name in script.deleted] == received
    assert script.trigger_calls == 2
    assert script.init_calls == 1 and script.exit_calls == 0
    assert triggers[0][0] is triggers[1][0]
    assert not pending
    backend._close_session(0)


def test_busy_without_exposure_waits_then_retry_owns_its_file(monkeypatch, tmp_path, recovery_state):
    backend, script, clock, pending, triggers, received = _body(monkeypatch, exposed=False)
    first, _ = backend.capture_image(tmp_path / "N.jpg", _config())
    assert Path(first).read_bytes() == b"PAGE_N.JPG"
    deadline = gb._capture_deadline_s(gb._parse_exposure_seconds("1/125"))
    assert deadline <= triggers[1][1] - triggers[0][1] < deadline + .5
    second, _ = backend.capture_image(tmp_path / "N1.jpg", _config())
    assert Path(second).read_bytes() == b"PAGE_N1.JPG"
    assert received == ["PAGE_N.JPG", "PAGE_N1.JPG"]
    assert script.trigger_calls == 3
    assert all(cam is triggers[0][0] for cam, at in triggers)
    assert script.init_calls == 1 and script.exit_calls == 0
    assert not pending
    backend._close_session(0)


@pytest.mark.parametrize("focus_check", [False, True])
def test_busy_startup_path_fails_without_fetching_it(monkeypatch, tmp_path, recovery_state, focus_check):
    old = _file_path(name="STARTUP.JPG")
    backend, script, gp, _ = _session(monkeypatch,
        before=[(GP_EVENT_FILE_ADDED, old, 0)],
        after=[(GP_EVENT_FILE_ADDED, old, .1)], trigger_error=True)
    monkeypatch.setattr(gp.Camera, "file_get", lambda *a: (_ for _ in ()).throw(AssertionError("startup file fetched")))
    with pytest.raises(RuntimeError, match="Take the page again"):
        backend.capture_image(tmp_path / "page.jpg", _config(), focus_check=focus_check)
    assert not script.saved_to and not script.deleted
    assert script.trigger_calls == 1 and script.exit_calls == 1
    assert not backend._sessions and not backend._claimed_ports


def test_healthy_cold_and_warm_captures_write_only_their_masters(monkeypatch, tmp_path, recovery_state):
    backend, script, _, _ = _session(monkeypatch)
    writes, extra_syncs = [], []
    import capture.utils as utils
    original_write, original_sync = gb.atomic_write, utils._fsync_dir

    def write(path, writer):
        writes.append(Path(path))
        return original_write(path, writer)

    def sync(path):
        extra_syncs.append(Path(path))
        return original_sync(path)

    monkeypatch.setattr(gb, "atomic_write", write)
    monkeypatch.setattr(utils, "_fsync_dir", sync)
    own = [tmp_path / "one.jpg", tmp_path / "two.jpg"]
    for path in own:
        backend.capture_image(path, _config())
    assert writes == own, f"writes outside the capture masters: {writes}"
    assert extra_syncs == [tmp_path, tmp_path], extra_syncs
    assert sorted(p.name for p in tmp_path.iterdir()) == ["one.jpg", "two.jpg"]
    backend._close_session(0)


@pytest.mark.parametrize("focus_check", [False, True])
@pytest.mark.parametrize("busy", [False, True])
def test_new_session_can_capture_the_same_path_without_extra_io(monkeypatch, tmp_path, recovery_state, focus_check, busy):
    path = _file_path()
    backend, script, gp, _ = _session(monkeypatch, before=[(GP_EVENT_FILE_ADDED, path, 0)], trigger_error=busy)
    first = backend._get_or_open_session(0)
    assert (path.folder, path.name) in first._unsolicited
    backend._close_session(0)
    gets, writes = [], []
    original_get, original_write = gp.Camera.file_get, gb.atomic_write

    def get(cam, folder, name, typ):
        gets.append((folder, name))
        return original_get(cam, folder, name, typ)

    def write(target, writer):
        writes.append(Path(target))
        return original_write(target, writer)

    monkeypatch.setattr(gp.Camera, "file_get", get)
    monkeypatch.setattr(gb, "atomic_write", write)
    result, _ = backend.capture_image(tmp_path / "page.jpg", _config(), focus_check=focus_check)
    assert Path(result).exists()
    assert gets == [(path.folder, path.name)]
    assert writes == [tmp_path / "page.jpg"]
    assert script.deleted == [(path.folder, path.name)]
    backend._close_session(0)
