"""A capture is reported only when the picture is whole, and no script is sent
over a running one.

What happened on 12 November 2026, from a USB recording of a Canon PowerShot
A2500 under CHDK 1.5.1 driven through pychdk 0.1.3:

- The capture script (init_usb_capture(1) ... shoot()) was still running on
  the camera for a short while after the host had received the picture's last
  chunk. pychdk returns the picture as soon as the data is in.
- The backend sent its next script, enter_alt(), 0.2 s after the last chunk,
  while that capture script was still running. CHDK's ExecuteScript kills a
  running script unless the request carries the NOKILL flag.
- The next shot's picture then arrived with its first chunk flagged last
  (response parameters 1461760/0/17920: more=0, seek to offset 17920).
  pychdk stopped there, zero-filled the first 17920 bytes and returned
  1,479,680 bytes, and the backend logged "remote capture ok".
- The next three captures each received one leftover chunk of that picture,
  with no exposure, and were logged as ok too.

A whole capture returns as soon as its picture has passed the completeness
check, and its script may still be running then. Every script the backend
sends waits first until no script is running, so the next one waits for it.
The fake body in chdk_fakes raises ScriptSentWhileRunning whenever a script
arrives while one is running, so every CHDK test enforces that rule.

Version: 0.1.0-beta
"""

import io
import logging
import struct

import pytest

import capture.backends.chdk_backend as cb
from capture.backends.errors import CaptureTimeoutError
from capture.camera import CameraConfig

from .chdk_fakes import Body, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
JPEG = b"\xff\xd8\xff\xe0 page \xff\xd9"
ENTER_ALT = "enter_alt()"
CLEAR_CAPTURE = "init_usb_capture(0)"


def _config(index=0):
    return CameraConfig(camera_index=index)


def _backend(monkeypatch, body):
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake, logger_name="test-chdk-whole")
    backend.list_devices()
    return backend, fake


def _canon_jpeg(file_number, model="Canon PowerShot A2500"):
    """A small real JPEG carrying a Canon maker note with a FileNumber.

    The maker note is one IFD entry, tag 0x0008 of type LONG with a count of
    one, in the byte order of the file's TIFF header, which is how the A2500's
    header chunks carried 1490830 and 1490831 on 12 November 2026.
    """
    from PIL import Image

    note = (
        struct.pack("<H", 1)
        + struct.pack("<HHII", 0x0008, 4, 1, file_number)
        + b"\x00\x00\x00\x00"
    )
    exif = Image.Exif()
    exif.endian = "<"
    exif[0x010F] = "Canon"
    exif[0x0110] = model
    exif.get_ifd(0x8769)[0x927C] = note
    out = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(out, "JPEG", exif=exif.tobytes())
    return out.getvalue()


def _in_chunks(picture, header=64):
    """Split a picture the way the A2500 delivered it: body first, header last.

    Every whole capture on 12 November 2026 had the shape
    1461760/1/17920; 1479168/1/-1; n/1/-1; 17920/0/0 - the data after the
    header first, at a seek past it, and the header last at offset 0.
    """
    middle = header + (len(picture) - header) // 2
    return [
        (picture[header:middle], True, header),
        (picture[middle:], True, -1),
        (picture[:header], False, 0),
    ]


def _backend_scripts(body):
    """The scripts the backend sent itself, not those the library sent."""
    return [s for s in body.scripts if s["flags"] is not None]


# --- A whole capture returns at once; the next script waits ---------------


def _waits(caplog, level):
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == level and "for a running script to end" in r.getMessage()
    ]


@pytest.mark.unit
def test_a_whole_capture_returns_without_waiting_for_its_script(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, capture_script_reads=5)
    backend, fake = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "page.jpg", _config())

    assert body.running_reads == 5, "the capture read the script status after its data"
    assert fake.sleeps == []
    assert (tmp_path / "page.jpg").read_bytes() == JPEG


@pytest.mark.unit
def test_enter_alt_before_the_next_capture_waits_for_the_capture_script(
    monkeypatch, tmp_path, caplog
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, capture_script_reads=4)
    backend, fake = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", _config())

    with caplog.at_level(logging.DEBUG, logger="test-chdk-whole"):
        backend.capture_image(tmp_path / "two.jpg", _config())

    assert [s["script"] for s in body.scripts].count("<capture>") == 2
    assert fake.sleeps == [cb._SCRIPT_POLL_INTERVAL_S] * 4
    waits = _waits(caplog, logging.DEBUG)
    assert len(waits) == 1
    assert "0.40s" in waits[0] and ENTER_ALT in waits[0]
    assert _waits(caplog, logging.WARNING) == []
    assert (tmp_path / "two.jpg").read_bytes() == JPEG


@pytest.mark.unit
def test_the_capture_script_waits_for_a_script_still_running(
    monkeypatch, tmp_path, caplog
):
    """The capture script is a script too.

    On a first capture the last Lua before the shot is get_zoom_steps(). This
    fake posts its answer at once and goes on running, so the capture script
    is what waits.
    """
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        script_reads_after_return={"get_zoom_steps": 3},
    )
    backend, _ = _backend(monkeypatch, body)

    with caplog.at_level(logging.DEBUG, logger="test-chdk-whole"):
        backend.capture_image(tmp_path / "page.jpg", _config())

    waits = _waits(caplog, logging.DEBUG)
    assert len(waits) == 1 and "the capture script" in waits[0]
    assert body.scripts[-1]["script"] == "<capture>"
    assert (tmp_path / "page.jpg").read_bytes() == JPEG


@pytest.mark.unit
def test_a_wait_over_two_seconds_is_a_warning(monkeypatch, tmp_path, caplog):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, capture_script_reads=25)
    backend, _ = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", _config())

    with caplog.at_level(logging.DEBUG, logger="test-chdk-whole"):
        backend.capture_image(tmp_path / "two.jpg", _config())

    warnings = _waits(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "2.50s" in warnings[0] and ENTER_ALT in warnings[0]
    assert (tmp_path / "two.jpg").read_bytes() == JPEG


@pytest.mark.unit
def test_capture_then_preview_reasserting_alt_waits_for_the_script(
    monkeypatch, tmp_path
):
    from .chdk_fakes import viewport_frame

    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG, frame=viewport_frame(),
        capture_script_reads=3,
    )
    backend, fake = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", _config())
    assert body.running_reads == 3
    fake.advance(cb._ALT_REASSERT_INTERVAL_S + 1)

    backend.capture_preview(0)

    assert body.scripts[-1]["script"] == ENTER_ALT
    assert body.running_reads == 0
    assert not any(s["while_running"] for s in body.scripts)


@pytest.mark.unit
def test_capture_then_zoom_waits_for_the_script(monkeypatch, tmp_path):
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64,
        capture_script_reads=3,
    )
    backend, _ = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", _config())
    assert body.running_reads == 3

    assert backend.set_zoom_step(0, 12) == 12

    assert body.zoom == 12
    assert body.running_reads == 0
    assert not any(s["while_running"] for s in body.scripts)


@pytest.mark.unit
def test_the_half_press_path_waits_for_the_half_press_script(monkeypatch, tmp_path):
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        switch_without_arriving=True, half_press_arrives=True,
        script_reads={'click("shoot_half")': 3},
    )
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "page.jpg", _config())

    assert body.half_presses == 1
    assert (tmp_path / "page.jpg").read_bytes() == JPEG
    assert not any(s["while_running"] for s in body.scripts)


@pytest.mark.unit
def test_a_capture_script_still_running_fails_the_next_capture(
    monkeypatch, tmp_path
):
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG, capture_script_reads=10**6
    )
    backend, fake = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", _config())
    sent = len(body.scripts)

    with pytest.raises(CaptureTimeoutError):
        backend.capture_image(tmp_path / "two.jpg", _config())

    assert len(body.scripts) == sent, "a script was sent over the capture script"
    assert body.scripts[-1]["script"] == "<capture>"
    assert backend._body_at(0) is None, "the body was kept"
    assert body.closes == 1
    assert not (tmp_path / "two.jpg").exists()
    assert sum(fake.sleeps) == pytest.approx(cb._SCRIPT_END_WAIT_S)


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["preview", "zoom"])
def test_a_capture_script_still_running_fails_the_next_operation(
    monkeypatch, tmp_path, operation
):
    from .chdk_fakes import viewport_frame

    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG, frame=viewport_frame(),
        zoom_steps=64, capture_script_reads=10**6,
    )
    backend, fake = _backend(monkeypatch, body)
    backend.capture_image(tmp_path / "one.jpg", _config())
    sent = len(body.scripts)
    fake.advance(cb._ALT_REASSERT_INTERVAL_S + 1)

    if operation == "preview":
        with pytest.raises(RuntimeError) as exc:
            backend.capture_preview(0)
        assert "still running" in str(exc.value)
    else:
        with pytest.raises(cb.ZoomFailedError) as exc:
            backend.set_zoom_step(0, 12)
        assert "still running" in str(exc.value)

    assert len(body.scripts) == sent
    assert backend._body_at(0) is None


@pytest.mark.unit
def test_a_script_already_running_is_waited_for_before_anything_is_sent(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, fake = _backend(monkeypatch, body)
    body._start_running("an earlier script", 5)

    backend.capture_image(tmp_path / "page.jpg", _config())

    assert body.scripts[0]["script"] == ENTER_ALT
    assert fake.sleeps[:5] == [cb._SCRIPT_POLL_INTERVAL_S] * 5


@pytest.mark.unit
def test_a_script_that_never_ends_fails_the_operation_and_sends_nothing(
    monkeypatch, tmp_path
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend, _ = _backend(monkeypatch, body)
    body._start_running("an earlier script", 10**6)

    with pytest.raises(CaptureTimeoutError):
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert body.scripts == [], "a script was sent over the running one"
    assert body.shots == []
    assert backend._body_at(0) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.unit
def test_every_script_the_backend_sends_carries_nokill(monkeypatch, tmp_path):
    """CHDK refuses a NOKILL script instead of killing a running one.

    The library's own shoot() and switch_mode() send their scripts without
    it, so for those two the status check before them is the only guard.
    """
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, zoom_steps=64)
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "page.jpg", _config())
    backend.set_zoom_step(0, 5)

    sent = _backend_scripts(body)
    assert [s["script"] for s in sent][:2] == [ENTER_ALT, "return get_mode()"]
    assert all(s["flags"] & cb._SCRIPT_NOKILL for s in sent)
    library = [s["script"] for s in body.scripts if s["flags"] is None]
    assert library == ["switch_mode_usb(1)", "<capture>"]


@pytest.mark.unit
def test_set_zoom_has_time_for_the_lens_to_travel(monkeypatch, tmp_path):
    """set_zoom is given _SET_ZOOM_TIMEOUT_S, longer than the default."""
    body = Body(
        serial="AAA111", card=EVEN_CARD, zoom_steps=64,
        script_reads={"set_zoom": int(12.0 / cb._LUA_POLL_INTERVAL_S)},
    )
    backend, _ = _backend(monkeypatch, body)

    assert backend.set_zoom_step(0, 40) == 40
    assert cb._SET_ZOOM_TIMEOUT_S == 15.0 > 12.0 > cb._LUA_TIMEOUT_S


# --- Part 2: a capture is reported only when the picture is whole ----------


def _b_to_e():
    """The 16:53 picture as it arrived: four chunks, each flagged last.

    B came first with 1461760 bytes at a seek to 17920; C and D were picture
    data with no seek; E was the 17920-byte header at offset 0. Only B was
    read by the capture that asked for it.
    """
    header = b"\xff\xd8\xff\xe1" + b"\x11" * (17920 - 4)
    b = b"\x5a" * 1461760
    c = b"\x6b" * 1479168
    d = b"\x7c" * (351939 - 2) + b"\xff\xd9"
    return [
        (b, False, 17920),
        (c, False, -1),
        (d, False, -1),
        (header, False, 0),
    ]


@pytest.mark.unit
def test_the_1653_partial_picture_is_refused_drained_and_the_next_is_clean(
    monkeypatch, tmp_path, caplog
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, shot_chunks=[_b_to_e()])
    backend, _ = _backend(monkeypatch, body)

    with caplog.at_level(logging.ERROR, logger="test-chdk-whole"):
        with pytest.raises(cb.IncompleteCaptureError) as exc:
            backend.capture_image(tmp_path / "one.jpg", _config())

    assert isinstance(exc.value, RuntimeError)
    message = str(exc.value)
    assert body.last_shot_chunks == [(1461760, False, 17920)]
    assert "start marker" in message
    assert "usb:001,004" in message
    assert not list(tmp_path.iterdir()), "the partial picture was written"
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("1 chunk" in e and "1479680 bytes" in e for e in errors), errors

    # The drain read the three leftover chunks and cleared the capture state.
    assert body.discarded_chunks == [
        (1479168, False, -1), (351939, False, -1), (17920, False, 0),
    ]
    assert body.ready_chunks == []
    assert body.scripts[-1]["script"] == CLEAR_CAPTURE
    assert body.scripts[-1]["flags"] & cb._SCRIPT_NOKILL
    assert backend._body_at(0) is not None, "a drained body was dropped"
    shots = len(body.shots)

    backend.capture_image(tmp_path / "two.jpg", _config())

    assert len(body.shots) == shots + 1, "the failed capture was retried"
    assert (tmp_path / "two.jpg").read_bytes() == JPEG


@pytest.mark.unit
@pytest.mark.parametrize(
    "chunk, wrong",
    [
        # The middle of a picture on its own: the 16:53 chunk C.
        ((b"\x6b" * 4096, False, -1), "start marker"),
        # The tail on its own, which does end with FF D9: the 16:53 chunk D.
        ((b"\x7c" * 4094 + b"\xff\xd9", False, -1), "start marker"),
        # The header chunk on its own: the 16:53 chunk E.
        ((b"\xff\xd8\xff\xe1" + b"\x11" * 4092, False, 0), "end marker"),
    ],
    ids=["mid-file", "tail-only", "header-only"],
)
def test_a_single_chunk_that_is_not_the_whole_picture_is_refused(
    monkeypatch, tmp_path, chunk, wrong
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG, shot_chunks=[[chunk]])
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(cb.IncompleteCaptureError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert wrong in str(exc.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.unit
def test_a_picture_without_its_end_marker_is_refused(monkeypatch, tmp_path):
    picture = _canon_jpeg(1490830)
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        shot_chunks=[_in_chunks(picture[:-2])],
    )
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(cb.IncompleteCaptureError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "end marker" in str(exc.value)
    assert not list(tmp_path.iterdir())


@pytest.mark.unit
def test_a_whole_picture_in_chunks_passes_unchanged(monkeypatch, tmp_path):
    first = _canon_jpeg(1490830)
    second = _canon_jpeg(1490831)
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        shot_chunks=[_in_chunks(first), _in_chunks(second)],
    )
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "one.jpg", _config())
    backend.capture_image(tmp_path / "two.jpg", _config())

    assert (tmp_path / "one.jpg").read_bytes() == first
    assert (tmp_path / "two.jpg").read_bytes() == second
    assert body.last_chunks == 3
    assert body.discarded_chunks == []
    assert CLEAR_CAPTURE not in [s["script"] for s in body.scripts]


@pytest.mark.unit
def test_the_same_file_number_twice_on_one_body_is_refused(monkeypatch, tmp_path):
    picture = _canon_jpeg(1490830)
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        shot_chunks=[_in_chunks(picture), _in_chunks(picture)],
    )
    backend, _ = _backend(monkeypatch, body)

    backend.capture_image(tmp_path / "one.jpg", _config())
    with pytest.raises(cb.IncompleteCaptureError) as exc:
        backend.capture_image(tmp_path / "two.jpg", _config())

    assert "1490830" in str(exc.value)
    assert not (tmp_path / "two.jpg").exists()


@pytest.mark.unit
def test_the_file_number_is_read_from_the_maker_note():
    assert cb._canon_file_number(_canon_jpeg(1490831)) == 1490831
    assert cb._canon_file_number(JPEG) is None
    assert cb._canon_file_number(b"\x00" * 64) is None


@pytest.mark.unit
def test_a_drain_that_keeps_finding_data_evicts_the_body(monkeypatch, tmp_path):
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        shot_chunks=[[(b"\x6b" * 4096, False, -1)]], always_ready=True,
    )
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(cb.IncompleteCaptureError):
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert len(body.discarded_chunks) == cb._DRAIN_MAX_CHUNKS == 8
    assert CLEAR_CAPTURE not in [s["script"] for s in body.scripts]
    assert backend._body_at(0) is None, "a body that could not be drained was kept"
    assert body.closes == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.unit
def test_a_drain_whose_clearing_script_never_ends_evicts_the_body(
    monkeypatch, tmp_path
):
    body = Body(
        serial="AAA111", card=EVEN_CARD, image=JPEG,
        shot_chunks=[[(b"\x6b" * 4096, False, -1)]],
        script_reads={CLEAR_CAPTURE: 10**6},
    )
    backend, _ = _backend(monkeypatch, body)

    with pytest.raises(cb.IncompleteCaptureError):
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert body.scripts[-1]["script"] == CLEAR_CAPTURE
    assert backend._body_at(0) is None
    assert body.closes == 1
