"""A DSLR capture must be bounded: trigger, then wait for the file event.

A single blocking gp.Camera.capture() call under the per-camera lock never
returns for a body that stalls (AF hunting, a wedged PTP transaction), so
previews and captures on that camera would freeze with no error and the retry
loop would re-issue the same blocking call.

The fakes here model the split libgphoto2 offers instead: trigger_capture()
returns immediately and wait_for_event(timeout_ms) delivers the file event in
bounded steps, so a deadline can be checked between calls. The fake Camera
deliberately has no capture() method, so a blocking capture path cannot pass.

Version: 0.1.0-beta
"""

import logging
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import capture.backends.gphoto2_backend as gb


MODEL = "Canon EOS Rebel T7"
PORT = "usb:001,005"
FOLDER = "/store_00020001/DCIM/100CANON"
NAME = "IMG_0001.JPG"
IMAGE_BYTES = b"\xff\xd8fake-jpeg-master"

# Fake libgphoto2 event constants (libgphoto2 2.5.34 values).
GP_EVENT_UNKNOWN = 0
GP_EVENT_TIMEOUT = 1
GP_EVENT_FILE_ADDED = 2
GP_EVENT_CAPTURE_COMPLETE = 4


class _Clock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Script:
    """Shared state for the fake camera: settings, scripted calls, counters."""

    def __init__(self, settings=None, triggers=None, events=None, clock=None):
        self.settings = {
            "capturetarget": "Internal RAM",
            "reviewtime": "None",
            "autopoweroff": "0",
            "flashmode": "Off",
            "focusmode": "Manual",
            "shutterspeed": "1/125",
        }
        self.settings.update(settings or {})
        # Each entry: None to succeed, or an exception instance to raise.
        self.triggers = list(triggers or [None])
        # Each entry: (event_type, event_data, seconds_spent) or an exception
        # instance to raise. The last entry repeats once the list is exhausted.
        self.events = list(events or [(GP_EVENT_FILE_ADDED, _file_path(), 0.0)])
        self.clock = clock
        self.init_calls = 0
        self.exit_calls = 0
        self.trigger_calls = 0
        # Clock reading at the moment each trigger_capture() call is made -
        # lets a test pin down *when* a retry fired, not just how many.
        self.trigger_times = []
        self.wait_calls = 0
        self.wait_timeouts_ms = []
        self.deleted = []
        self.saved_to = []

    def spend(self, seconds):
        """Consume `seconds` of the clock the deadline is measured against."""
        if self.clock is not None:
            self.clock.advance(seconds)
        elif seconds:
            time.sleep(seconds)


def _file_path(folder=FOLDER, name=NAME):
    """Stand-in for gp.CameraFilePath (.folder / .name)."""
    return SimpleNamespace(folder=folder, name=name)


class _Widget:
    def __init__(self, settings, key):
        self._settings = settings
        self._key = key

    def get_value(self):
        return self._settings[self._key]

    def set_value(self, value):
        self._settings[self._key] = value


def _make_fake_gp(script):
    class GPhoto2Error(Exception):
        pass

    class _Config:
        def get_child_by_name(self, name):
            if name not in script.settings:
                raise GPhoto2Error(f"[-2] Bad parameters: unknown widget {name!r}")
            return _Widget(script.settings, name)

    class _CameraFile:
        def save(self, path):
            script.saved_to.append(path)
            Path(path).write_bytes(IMAGE_BYTES)

    class Camera:
        # NOTE: no capture() method. The bounded path must not call it.

        @staticmethod
        def autodetect():
            return [(MODEL, PORT)]

        def set_abilities(self, abilities):
            pass

        def set_port_info(self, port_info):
            pass

        def init(self):
            script.init_calls += 1

        def exit(self):
            script.exit_calls += 1

        def get_config(self):
            return _Config()

        def set_config(self, cfg):
            pass

        def trigger_capture(self):
            script.trigger_times.append(
                script.clock.now if script.clock is not None else None
            )
            idx = min(script.trigger_calls, len(script.triggers) - 1)
            script.trigger_calls += 1
            outcome = script.triggers[idx]
            if isinstance(outcome, BaseException):
                raise outcome

        def wait_for_event(self, timeout_ms):
            idx = min(script.wait_calls, len(script.events) - 1)
            script.wait_calls += 1
            script.wait_timeouts_ms.append(timeout_ms)
            entry = script.events[idx]
            if isinstance(entry, BaseException):
                raise entry
            evt, data, spent = entry
            script.spend(spent)
            return evt, data

        def file_get(self, folder, name, file_type):
            return _CameraFile()

        def file_delete(self, folder, name):
            script.deleted.append((folder, name))

    class CameraAbilitiesList:
        def load(self):
            pass

        def lookup_model(self, model):
            return 0

        def __getitem__(self, idx):
            return object()

    class PortInfoList:
        def load(self):
            pass

        def lookup_path(self, path):
            return 0

        def __getitem__(self, idx):
            return object()

    return types.SimpleNamespace(
        GPhoto2Error=GPhoto2Error,
        Camera=Camera,
        CameraAbilitiesList=CameraAbilitiesList,
        PortInfoList=PortInfoList,
        GP_CAPTURE_IMAGE=0,
        GP_FILE_TYPE_NORMAL=1,
        GP_EVENT_UNKNOWN=GP_EVENT_UNKNOWN,
        GP_EVENT_TIMEOUT=GP_EVENT_TIMEOUT,
        GP_EVENT_FILE_ADDED=GP_EVENT_FILE_ADDED,
        GP_EVENT_CAPTURE_COMPLETE=GP_EVENT_CAPTURE_COMPLETE,
    )


def _make_backend(monkeypatch, script):
    fake_gp = _make_fake_gp(script)
    monkeypatch.setattr(gb, "gp", fake_gp)
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb, "_RAWPY_AVAILABLE", False)
    backend = gb.GPhoto2Backend(logging.getLogger("test-gphoto2-capture-timeout"))
    return backend, fake_gp


def _config():
    """CameraConfig stand-in with only camera_index, so nothing is set."""
    return SimpleNamespace(camera_index=0)


# ----------------------------------------------------------------------
# (a) a camera that never delivers the file must fail, not hang
# ----------------------------------------------------------------------

def test_capture_times_out_when_no_file_event_arrives(monkeypatch, tmp_path):
    script = _Script(
        settings={"shutterspeed": "1/125"},
        events=[(GP_EVENT_TIMEOUT, None, 0.05)],
    )
    backend, _ = _make_backend(monkeypatch, script)
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.3)

    threads_before = threading.active_count()
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out|no image after"):
        backend.capture_image(tmp_path / "x.jpg", _config())
    wall = time.monotonic() - t0

    assert wall < 2.0, f"capture took {wall:.2f}s; the deadline did not bound it"
    assert script.trigger_calls == 1
    assert script.wait_calls >= 1, "the wait must poll in steps, not block once"
    assert 0 not in backend._sessions, "a timed-out session must be closed"
    assert script.exit_calls == 1
    # <= rather than ==: an unrelated thread ending between the two counts is
    # not a leak; a new thread surviving the timeout is.
    assert threading.active_count() <= threads_before, "no watchdog thread may leak"


# ----------------------------------------------------------------------
# (b) the happy path: other events are skipped, FILE_ADDED is downloaded
# ----------------------------------------------------------------------

def test_capture_returns_the_file_delivered_by_the_event_loop(monkeypatch, tmp_path):
    path = _file_path()
    script = _Script(
        events=[
            (GP_EVENT_CAPTURE_COMPLETE, None, 0.0),
            (GP_EVENT_FILE_ADDED, path, 0.0),
        ],
    )
    backend, _ = _make_backend(monkeypatch, script)

    outpath = tmp_path / "page_0001.jpg"
    result = backend.capture_image(outpath, _config())

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved == outpath
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.deleted == [(FOLDER, NAME)]
    assert script.trigger_calls == 1
    assert script.wait_calls == 2
    assert script.wait_timeouts_ms == [gb._EVENT_STEP_MS, gb._EVENT_STEP_MS]


# ----------------------------------------------------------------------
# (c) only the trigger is retried, on transient I/O-busy errors
# ----------------------------------------------------------------------

def test_trigger_is_retried_only_after_the_capture_deadline_is_empty(
    monkeypatch, tmp_path
):
    # The post-trigger-error poll waits out the full capture deadline
    # (the same one _wait_for_file_added uses), not a fixed settle delay -
    # a busy error can follow a successful exposure whose file may take as
    # long as the deadline to land. Drive it with a fake clock: a run of
    # TIMEOUT events pushes the clock past the deadline before the trigger
    # is retried; the FILE_ADDED event after that is picked up by the
    # normal post-trigger wait.
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    path = _file_path()
    script = _Script(
        settings={"shutterspeed": "1/125"},
        events=[
            (GP_EVENT_TIMEOUT, None, 5.0),
            (GP_EVENT_TIMEOUT, None, 5.0),
            (GP_EVENT_TIMEOUT, None, 5.0),
            (GP_EVENT_TIMEOUT, None, 5.0),
            (GP_EVENT_FILE_ADDED, path, 0.0),
        ],
        clock=clock,
    )
    backend, fake_gp = _make_backend(monkeypatch, script)
    script.triggers = [fake_gp.GPhoto2Error("[-110] I/O in progress"), None]

    slept = []
    monkeypatch.setattr(gb.time, "sleep", lambda s: slept.append(s))

    outpath = tmp_path / "page_0002.jpg"
    result = backend.capture_image(outpath, _config())

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.trigger_calls == 2, "the trigger should be retried once"
    assert slept == [], "the wait must poll the event queue, not sleep"
    assert script.deleted == [(FOLDER, NAME)], "exactly one image was taken"
    assert 0 in backend._sessions, "a recovered capture keeps the session open"

    deadline = gb._capture_deadline_s(gb._parse_exposure_seconds("1/125"))
    assert script.trigger_times[1] - script.trigger_times[0] >= deadline, (
        "the retry must not fire before the full capture deadline has elapsed "
        "since the previous trigger attempt"
    )


# ----------------------------------------------------------------------
# (c2) a file seen during the capture-deadline poll means the exposure
# already happened - do not re-trigger
# ----------------------------------------------------------------------

def test_trigger_retry_returns_the_file_seen_during_the_capture_deadline(
    monkeypatch, tmp_path
):
    # A 30s exposure sizes a 75s deadline; the file lands at t=30s, well
    # inside it but well outside any short fixed settle delay, so the busy
    # trigger error must not cause a second photograph.
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    path = _file_path()
    script = _Script(
        settings={"shutterspeed": "30"},
        events=[
            (GP_EVENT_TIMEOUT, None, 15.0),
            (GP_EVENT_TIMEOUT, None, 15.0),
            (GP_EVENT_FILE_ADDED, path, 0.0),
        ],
        clock=clock,
    )
    backend, fake_gp = _make_backend(monkeypatch, script)
    script.triggers = [fake_gp.GPhoto2Error("[-110] I/O in progress"), None]

    slept = []
    monkeypatch.setattr(gb.time, "sleep", lambda s: slept.append(s))

    outpath = tmp_path / "page_0002b.jpg"
    result = backend.capture_image(outpath, _config())

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.trigger_calls == 1, (
        "the exposure already happened during the capture-deadline poll; "
        "re-triggering would take a second photo"
    )
    assert script.deleted == [(FOLDER, NAME)], "exactly one image was taken"
    assert slept == []
    assert clock.now == pytest.approx(1030.0)


# ----------------------------------------------------------------------
# (c3) every attempt stays busy with an empty queue - each non-final
# attempt must wait out a full capture deadline before the next retry
# ----------------------------------------------------------------------

def test_trigger_retry_raises_after_repeated_empty_deadlines(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _Script(
        settings={"shutterspeed": "1/125"},
        events=[(GP_EVENT_TIMEOUT, None, 5.0)],
        clock=clock,
    )
    backend, fake_gp = _make_backend(monkeypatch, script)
    error = fake_gp.GPhoto2Error("[-110] I/O in progress")
    script.triggers = [error, error, error]

    slept = []
    monkeypatch.setattr(gb.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(RuntimeError, match="DSLR capture failed"):
        backend.capture_image(tmp_path / "page_busy.jpg", _config())

    assert script.trigger_calls == 3, "all `retry` attempts must be used"
    assert slept == [], "the wait must poll the event queue, not sleep"
    assert 0 not in backend._sessions, "a failed capture must close the session"

    deadline = gb._capture_deadline_s(gb._parse_exposure_seconds("1/125"))
    # Two full deadline windows are waited out (after attempts 1 and 2)
    # before attempt 3 raises without polling again - each retry is spaced
    # by roughly a full capture deadline, not a short fixed settle delay.
    assert len(script.trigger_times) == 3
    assert script.trigger_times[1] - script.trigger_times[0] >= deadline
    assert script.trigger_times[2] - script.trigger_times[1] >= deadline


# ----------------------------------------------------------------------
# (c4) a file seen during the LAST attempt's post-error poll must still
# be returned - the recovery poll is not gated by attempts remaining
# ----------------------------------------------------------------------

def test_trigger_retry_returns_file_seen_on_the_last_attempts_poll(
    monkeypatch, tmp_path
):
    # Two full empty deadlines (after attempts 1 and 2) are waited out and
    # the trigger is retried each time; on attempt 3 - the last one, with
    # no retries left - the trigger is busy again, but the file turns up
    # partway through that same attempt's recovery poll. Even with no
    # attempts remaining, the file must be returned rather than the
    # original error re-raised: a retriable error on the last attempt is
    # exactly as ambiguous about whether the shutter fired as one on any
    # earlier attempt, so it gets exactly the same recovery poll.
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    path = _file_path()
    script = _Script(
        settings={"shutterspeed": "1/125"},
        events=[(GP_EVENT_TIMEOUT, None, 5.0)] * 10
        + [(GP_EVENT_FILE_ADDED, path, 0.0)],
        clock=clock,
    )
    backend, fake_gp = _make_backend(monkeypatch, script)
    error = fake_gp.GPhoto2Error("[-110] I/O in progress")
    script.triggers = [error, error, error]

    slept = []
    monkeypatch.setattr(gb.time, "sleep", lambda s: slept.append(s))

    outpath = tmp_path / "page_last_attempt_recovers.jpg"
    result = backend.capture_image(outpath, _config())

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.trigger_calls == 3, (
        "all three attempts were tried and no fourth trigger was issued"
    )
    assert slept == [], "the wait must poll the event queue, not sleep"
    assert script.deleted == [(FOLDER, NAME)], "exactly one image was taken"
    assert 0 in backend._sessions, "a recovered capture keeps the session open"


# ----------------------------------------------------------------------
# (d) an error out of the event wait is fatal - never re-trigger
# ----------------------------------------------------------------------

def test_event_wait_error_is_not_retried(monkeypatch, tmp_path):
    script = _Script()
    backend, fake_gp = _make_backend(monkeypatch, script)
    script.events = [fake_gp.GPhoto2Error("[-7] I/O problem")]

    with pytest.raises(RuntimeError, match="DSLR capture failed"):
        backend.capture_image(tmp_path / "page_0003.jpg", _config())

    assert script.trigger_calls == 1, (
        "re-triggering after the shutter fired would take a second photo"
    )
    assert script.deleted == []
    assert 0 not in backend._sessions
    assert script.exit_calls == 1


# ----------------------------------------------------------------------
# (e) the deadline arithmetic, as pure functions
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "shutterspeed,expected",
    [
        ("1/125", 1 / 125),
        ("1/4000", 1 / 4000),
        ("0.8", 0.8),
        ("1.6", 1.6),
        ("30", 30.0),
        ("20.3", 20.3),
        ("bulb", 30.0),
        ("Bulb", 30.0),
        ("  30  ", 30.0),
        ("auto", 30.0),
        ("", 30.0),
        (None, 30.0),
        ("garbage", 30.0),
    ],
)
def test_parse_exposure_seconds(shutterspeed, expected):
    assert gb._parse_exposure_seconds(shutterspeed) == pytest.approx(expected)


def test_unknown_exposure_uses_the_module_constant():
    assert gb._parse_exposure_seconds("bulb") == gb._UNKNOWN_EXPOSURE_S
    assert gb._parse_exposure_seconds(None) == gb._UNKNOWN_EXPOSURE_S
    assert gb._parse_exposure_seconds("garbage") == gb._UNKNOWN_EXPOSURE_S


@pytest.mark.parametrize(
    "exposure,expected",
    [
        (30.0, 75.0),
        (0.008, 15.016),
        (0.0, 15.0),
    ],
)
def test_capture_deadline_scales_with_exposure(exposure, expected):
    assert gb._capture_deadline_s(exposure) == pytest.approx(expected)


def test_capture_deadline_base_is_the_module_constant():
    assert gb._capture_deadline_s(0.0) == gb._CAPTURE_TIMEOUT_BASE_S


# ----------------------------------------------------------------------
# an unreadable shutterspeed (failed config read) must not shorten the
# deadline - it gets the same conservative deadline as "bulb"
# ----------------------------------------------------------------------

def test_unreadable_shutterspeed_gets_the_conservative_deadline(monkeypatch, tmp_path):
    clock = _Clock()
    path = _file_path()
    script = _Script(
        settings={"shutterspeed": None},
        events=[
            (GP_EVENT_TIMEOUT, None, 20.0),
            (GP_EVENT_TIMEOUT, None, 20.0),
            (GP_EVENT_TIMEOUT, None, 20.0),
            (GP_EVENT_FILE_ADDED, path, 0.0),
        ],
        clock=clock,
    )
    backend, _ = _make_backend(monkeypatch, script)
    monkeypatch.setattr(gb.time, "monotonic", clock)

    # A None shutterspeed maps to _UNKNOWN_EXPOSURE_S (30s), giving the same
    # 75s deadline as an actual 30s exposure, so the file at t=60 still
    # arrives in time.
    result = backend.capture_image(tmp_path / "page_none.jpg", _config())

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.wait_calls == 4
    assert clock.now == pytest.approx(1060.0)


# ----------------------------------------------------------------------
# (f) a long exposure gets a longer deadline; a short one does not
# ----------------------------------------------------------------------

def test_long_exposure_is_given_time_to_finish(monkeypatch, tmp_path):
    clock = _Clock()
    path = _file_path()
    script = _Script(
        settings={"shutterspeed": "30"},
        events=[
            (GP_EVENT_TIMEOUT, None, 20.0),
            (GP_EVENT_TIMEOUT, None, 20.0),
            (GP_EVENT_TIMEOUT, None, 20.0),
            (GP_EVENT_FILE_ADDED, path, 0.0),
        ],
        clock=clock,
    )
    backend, _ = _make_backend(monkeypatch, script)
    monkeypatch.setattr(gb.time, "monotonic", clock)

    # Deadline for a 30 s exposure is 75 s; the file lands at t = 60 s.
    result = backend.capture_image(tmp_path / "page_0004.jpg", _config())

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.wait_calls == 4
    assert clock.now == pytest.approx(1060.0)


def test_short_exposure_times_out_at_the_base_deadline(monkeypatch, tmp_path):
    clock = _Clock()
    script = _Script(
        settings={"shutterspeed": "1/125"},
        events=[(GP_EVENT_TIMEOUT, None, 20.0)],
        clock=clock,
    )
    backend, _ = _make_backend(monkeypatch, script)
    monkeypatch.setattr(gb.time, "monotonic", clock)

    # Deadline is ~15 s, so the first check past it fires on the first step.
    with pytest.raises(RuntimeError, match="timed out|no image after"):
        backend.capture_image(tmp_path / "page_0005.jpg", _config())

    assert script.wait_calls == 1
    assert 0 not in backend._sessions


# ----------------------------------------------------------------------
# (g) the timeout message names AF only when AF is actually on
# ----------------------------------------------------------------------

def _timeout_message(monkeypatch, tmp_path, focusmode, name):
    script = _Script(
        settings={"focusmode": focusmode, "shutterspeed": "1/125"},
        events=[(GP_EVENT_TIMEOUT, None, 0.02)],
    )
    backend, _ = _make_backend(monkeypatch, script)
    monkeypatch.setattr(gb, "_CAPTURE_TIMEOUT_BASE_S", 0.1)

    with pytest.raises(RuntimeError) as excinfo:
        backend.capture_image(tmp_path / name, _config())
    return str(excinfo.value)


def test_timeout_message_points_at_af_when_the_lens_is_not_in_mf(
    monkeypatch, tmp_path
):
    message = _timeout_message(monkeypatch, tmp_path, "One Shot", "af.jpg")

    assert "flip the lens barrel switch to MF" in message
    assert "One Shot" in message
    assert PORT in message
    assert "1/125" in message


def test_timeout_message_omits_the_af_hint_in_manual_focus(monkeypatch, tmp_path):
    message = _timeout_message(monkeypatch, tmp_path, "Manual", "mf.jpg")

    assert "flip the lens barrel switch to MF" not in message
    assert PORT in message


# ----------------------------------------------------------------------
# a file that arrives after the deadline has already passed is still a
# good capture - the deadline only bounds how long an empty queue is
# waited on, not whether a delivered file is accepted
# ----------------------------------------------------------------------

def test_late_file_is_still_accepted(monkeypatch, tmp_path):
    clock = _Clock()
    path = _file_path()
    script = _Script(
        settings={"shutterspeed": "1/125"},  # deadline stays near the 15s base
        events=[
            (GP_EVENT_TIMEOUT, None, 10.0),
            (GP_EVENT_FILE_ADDED, path, 20.0),
        ],
        clock=clock,
    )
    backend, _ = _make_backend(monkeypatch, script)
    monkeypatch.setattr(gb.time, "monotonic", clock)

    # Deadline is ~15s; the file lands at t=30 (after the 10s TIMEOUT step
    # plus the 20s spent delivering FILE_ADDED) and must still be accepted.
    result = backend.capture_image(tmp_path / "late.jpg", _config())

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.wait_calls == 2


# ----------------------------------------------------------------------
# (h) NEH-339: a focus check takes one trigger attempt under a 20 s
# deadline; every other capture keeps the normal policy
# ----------------------------------------------------------------------

def _cause_chain(exc):
    chain = []
    current = exc
    while current is not None and len(chain) < 5:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _busy_script(clock, shutterspeed="1/125", events=None):
    return _Script(
        settings={"shutterspeed": shutterspeed, "focusmode": "One Shot"},
        events=events or [(GP_EVENT_TIMEOUT, None, 0.5)],
        clock=clock,
    )


def test_normal_capture_keeps_the_retry_count_and_deadline(monkeypatch, tmp_path):
    # Pins the policy a capture without focus_check has always had: three
    # trigger attempts, each busy attempt but the last followed by a poll of
    # the full exposure-sized deadline, and the plain trigger error (not a
    # timeout) once the attempts run out.
    assert gb._DEFAULT_RETRY == 3
    assert gb._CAPTURE_TIMEOUT_BASE_S == 15.0
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _busy_script(clock)
    backend, fake_gp = _make_backend(monkeypatch, script)
    error = fake_gp.GPhoto2Error("[-110] I/O in progress")
    script.triggers = [error, error, error]

    with pytest.raises(RuntimeError, match="DSLR capture failed") as excinfo:
        backend.capture_image(tmp_path / "normal_busy.jpg", _config())

    assert script.trigger_calls == 3
    assert not any(
        isinstance(e, gb.CaptureTimeoutError) for e in _cause_chain(excinfo.value)
    ), "a busy trigger outside a focus check stays a trigger error"
    deadline = gb._capture_deadline_s(gb._parse_exposure_seconds("1/125"))
    assert deadline == pytest.approx(15.0 + 2 / 125)
    gaps = [b - a for a, b in zip(script.trigger_times, script.trigger_times[1:])]
    assert all(deadline <= gap < deadline + 0.5 for gap in gaps), gaps


def test_normal_capture_waits_the_exposure_sized_deadline(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _Script(
        settings={"shutterspeed": "30"},
        events=[(GP_EVENT_TIMEOUT, None, 0.5)],
        clock=clock,
    )
    backend, _ = _make_backend(monkeypatch, script)

    with pytest.raises(RuntimeError, match="no image after"):
        backend.capture_image(tmp_path / "normal_slow.jpg", _config())

    # 15 + 2 * 30 = 75 s, not the focus-check 20 s.
    assert 75.0 <= clock.now - 1000.0 < 75.5
    assert script.trigger_calls == 1


def test_focus_check_false_is_the_normal_capture(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _busy_script(clock)
    backend, fake_gp = _make_backend(monkeypatch, script)
    error = fake_gp.GPhoto2Error("[-110] I/O in progress")
    script.triggers = [error, error, error]

    with pytest.raises(RuntimeError, match="DSLR capture failed"):
        backend.capture_image(tmp_path / "explicit_false.jpg", _config(), focus_check=False)

    assert script.trigger_calls == 3


def test_focus_check_deadline_is_the_module_constant():
    assert gb._FOCUS_CHECK_DEADLINE_S == 20.0


def test_focus_check_returns_the_file(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _Script(
        events=[(GP_EVENT_TIMEOUT, None, 4.0), (GP_EVENT_FILE_ADDED, _file_path(), 0.0)],
        clock=clock,
    )
    backend, _ = _make_backend(monkeypatch, script)

    result = backend.capture_image(tmp_path / "fc_ok.jpg", _config(), focus_check=True)

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.trigger_calls == 1
    assert script.deleted == [(FOLDER, NAME)]
    assert 0 in backend._sessions


def test_focus_check_times_out_at_20_s_whatever_the_exposure(monkeypatch, tmp_path):
    # A "30" shutterspeed would size a 75 s deadline; the focus check
    # gives up at 20 s from the trigger regardless.
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _busy_script(clock, shutterspeed="30")
    backend, _ = _make_backend(monkeypatch, script)

    with pytest.raises(RuntimeError) as excinfo:
        backend.capture_image(tmp_path / "fc_slow.jpg", _config(), focus_check=True)

    assert any(isinstance(e, gb.CaptureTimeoutError) for e in _cause_chain(excinfo.value))
    assert 20.0 <= clock.now - script.trigger_times[0] < 20.5
    assert script.trigger_calls == 1
    message = str(excinfo.value)
    assert "focus-check deadline" in message
    assert PORT in message
    assert "flip the lens barrel switch to MF" not in message, (
        "a focus check runs on AF by design; the MF hint does not apply"
    )
    assert 0 not in backend._sessions, "a timed-out session is closed as before"


def test_focus_check_busy_trigger_is_one_attempt_then_a_timeout(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _busy_script(clock)
    backend, fake_gp = _make_backend(monkeypatch, script)
    error = fake_gp.GPhoto2Error("[-110] I/O in progress")
    script.triggers = [error, error, error]

    with pytest.raises(RuntimeError) as excinfo:
        backend.capture_image(tmp_path / "fc_busy.jpg", _config(), focus_check=True)

    assert script.trigger_calls == 1, "a focus check never re-triggers"
    assert any(isinstance(e, gb.CaptureTimeoutError) for e in _cause_chain(excinfo.value))
    assert 20.0 <= clock.now - script.trigger_times[0] < 20.5
    message = str(excinfo.value)
    assert "no picture arrived within the focus-check deadline" in message
    assert PORT in message
    assert 0 not in backend._sessions
    assert script.exit_calls == 1


def test_focus_check_busy_trigger_returns_a_file_that_lands_in_time(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _busy_script(
        clock,
        events=[(GP_EVENT_TIMEOUT, None, 6.0), (GP_EVENT_FILE_ADDED, _file_path(), 0.0)],
    )
    backend, fake_gp = _make_backend(monkeypatch, script)
    script.triggers = [fake_gp.GPhoto2Error("[-110] I/O in progress"), None]

    result = backend.capture_image(tmp_path / "fc_busy_ok.jpg", _config(), focus_check=True)

    saved = Path(result[0] if isinstance(result, tuple) else result)
    assert saved.read_bytes() == IMAGE_BYTES
    assert script.trigger_calls == 1
    assert script.deleted == [(FOLDER, NAME)]


def test_focus_check_non_busy_trigger_error_is_not_a_timeout(monkeypatch, tmp_path):
    clock = _Clock()
    monkeypatch.setattr(gb.time, "monotonic", clock)
    script = _busy_script(clock)
    backend, fake_gp = _make_backend(monkeypatch, script)
    script.triggers = [fake_gp.GPhoto2Error("[-7] I/O problem")]

    with pytest.raises(RuntimeError, match="DSLR capture failed") as excinfo:
        backend.capture_image(tmp_path / "fc_err.jpg", _config(), focus_check=True)

    assert script.trigger_calls == 1
    assert not any(
        isinstance(e, gb.CaptureTimeoutError) for e in _cause_chain(excinfo.value)
    )
    assert clock.now == 1000.0, "a non-busy trigger error is raised without waiting"
