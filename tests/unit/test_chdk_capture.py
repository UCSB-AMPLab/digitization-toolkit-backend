"""Capturing a page over USB, and refusing to when the rig is not ready.

CHDK's remote capture hands the JPEG straight down the wire, so the picture
never touches the card. What can go wrong is bounded and each failure has to
arrive carrying its own evidence: the bench is the only session with hardware,
and "capture failed" in a log costs that session.

Two refusals happen before the shutter. A body with no identity may not
capture, because nothing it produces could be attributed to a body; and
neither may a body whose parity another body also claims, because one of the
two would be filed as the wrong page.
"""

import pytest

from capture.backends.errors import CaptureTimeoutError
from capture.camera import CameraConfig

from .chdk_fakes import Body, PTPError, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"
JPEG = b"\xff\xd8\xff\xe0 pretend this is a page \xff\xd9"
ODD_JPEG = b"\xff\xd8\xff\xe0 pretend this is an odd page \xff\xd9"


def _config(index=0, **kwargs):
    return CameraConfig(camera_index=index, **kwargs)


@pytest.mark.unit
def test_a_capture_writes_the_bytes_and_reports_the_path(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    path, metadata = backend.capture_image(tmp_path / "page.jpg", _config())

    assert metadata is None
    assert path == str(tmp_path / "page.jpg")
    assert (tmp_path / "page.jpg").read_bytes() == JPEG
    assert list(tmp_path.iterdir()) == [tmp_path / "page.jpg"], "a temp file was left"
    assert body.shots[0]["stream"] is True, "the card must not be involved"


@pytest.mark.unit
def test_an_output_name_that_is_not_a_jpeg_is_refused(monkeypatch, tmp_path):
    """Two places deciding a filename is how a page gets overwritten.

    The capture service picks the name and guarantees it does not already
    exist. Renaming it here to the only format remote capture returns would
    move the write to a name nothing checked - page.cr2 comes in, page.jpg
    goes out, and the page already under that name is replaced by an atomic
    write that is atomic about the wrong thing. The service owns the name, so
    a name this backend cannot honour is refused before the shutter fires.
    """
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()
    (tmp_path / "page.jpg").write_bytes(b"a page that already exists")

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.cr2", _config())

    assert ".cr2" in str(exc.value)
    assert body.shots == [], "the shutter fired for a file that was refused"
    assert (tmp_path / "page.jpg").read_bytes() == b"a page that already exists"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["page.jpg", "page.JPG", "page.jpeg"])
def test_the_file_is_written_under_the_name_it_was_given(
    monkeypatch, tmp_path, name
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    path, _ = backend.capture_image(tmp_path / name, _config())

    assert path == str(tmp_path / name)
    assert (tmp_path / name).read_bytes() == JPEG


@pytest.mark.unit
def test_record_mode_is_switched_once_and_then_remembered(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    backend.capture_image(tmp_path / "one.jpg", _config())
    backend.capture_image(tmp_path / "two.jpg", _config())

    assert body.mode_switches == ["record"]


@pytest.mark.unit
def test_the_shutter_string_reaches_the_library_as_seconds(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    backend.capture_image(
        tmp_path / "page.jpg", _config(shutter_speed="1/250", iso=400)
    )

    assert body.shots[0]["shutter_speed"] == pytest.approx(1 / 250)
    assert body.shots[0]["market_iso"] == 400


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "auto", "bulb", "", "nonsense", "0"])
def test_a_shutter_speed_that_names_no_duration_is_left_to_the_body(
    monkeypatch, tmp_path, value
):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    backend.capture_image(tmp_path / "page.jpg", _config(shutter_speed=value))

    assert body.shots[0]["shutter_speed"] is None


@pytest.mark.unit
def test_a_body_that_never_delivers_raises_the_shared_timeout(monkeypatch, tmp_path):
    """The route turns this class into a 504; a plain RuntimeError is a 500."""
    body = Body(
        serial="AAA111", card=EVEN_CARD,
        shoot_error=TimeoutError("Remote capture did not complete"),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(CaptureTimeoutError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "usb:001,004" in str(exc.value)
    assert not list(tmp_path.iterdir()), "a failed capture left a file behind"


@pytest.mark.unit
def test_a_ptp_failure_names_its_code_and_drops_the_body(monkeypatch, tmp_path):
    """0x2002 from remote capture is this row's load-bearing risk (NEH-231)."""
    body = Body(
        serial="AAA111", card=EVEN_CARD, shoot_error=PTPError(0x2002),
    )
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    message = str(exc.value)
    assert "PTP 0x2002" in message
    assert "usb:001,004" in message
    assert not isinstance(exc.value, CaptureTimeoutError)
    assert body.closes == 1, "a body that failed over PTP was left open"
    assert backend._body_at(0) is None, "the failed body is still in the map"


@pytest.mark.unit
def test_a_body_dropped_by_a_failure_comes_back_on_the_next_scan(
    monkeypatch, tmp_path, caplog
):
    body = Body(serial="AAA111", card=EVEN_CARD, shoot_error=PTPError(0x2002))
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError):
        backend.capture_image(tmp_path / "page.jpg", _config())

    with caplog.at_level("INFO", logger="test-chdk"):
        rows = backend.list_devices()

    assert [row["index"] for row in rows] == [0]
    assert body.opens == 2
    line = "\n".join(record.getMessage() for record in caplog.records)
    assert "re-enumeration recovered it" in line


@pytest.mark.unit
def test_a_body_with_no_identity_refuses_to_capture(monkeypatch, tmp_path):
    body = Body(serial=None, card=b"EVEN\n", image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config())

    assert "/side/" in str(exc.value)
    assert body.shots == [], "the shutter fired on a body with no identity"


@pytest.mark.unit
def test_two_bodies_on_one_parity_refuse_to_capture(monkeypatch, tmp_path):
    first = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD, image=JPEG)
    second = Body(bus=1, address=7, serial="BBB222", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(first, second))
    backend.list_devices()

    for index in (0, 1):
        with pytest.raises(RuntimeError) as exc:
            backend.capture_image(tmp_path / f"page{index}.jpg", _config(index))
        assert "even" in str(exc.value)

    assert first.shots == [] and second.shots == []


@pytest.mark.unit
def test_a_contested_parity_does_not_file_an_odd_body_at_the_even_index(
    monkeypatch, tmp_path
):
    """Two EVEN bodies argue; the ODD body must not be pulled into the row.

    The ODD body's parity is its own and it carries no refusal, so whatever
    index it holds is the index the service captures from. On index 0 it
    shoots an odd page and the service files it as an even one - which is the
    whole failure this backend exists to prevent, arriving through a fault
    that belongs to two other bodies.
    """
    odd = Body(bus=1, address=4, serial="AAA111", card=ODD_CARD, image=ODD_JPEG)
    first_even = Body(
        bus=1, address=7, serial="BBB222", card=EVEN_CARD, image=JPEG
    )
    second_even = Body(
        bus=1, address=9, serial="CCC333",
        card=b"EVEN\nid=cccccccccccc\n", image=JPEG,
    )
    backend = make_backend(
        monkeypatch, make_pychdk(odd, first_even, second_even)
    )
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "even.jpg", _config(0))

    assert "even" in str(exc.value)
    assert not (tmp_path / "even.jpg").exists()

    backend.capture_image(tmp_path / "odd.jpg", _config(1))

    assert (tmp_path / "odd.jpg").read_bytes() == ODD_JPEG
    assert len(odd.shots) == 1
    assert first_even.shots == [] and second_even.shots == []


@pytest.mark.unit
def test_capturing_from_an_index_with_no_body_says_so(monkeypatch, tmp_path):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with pytest.raises(RuntimeError) as exc:
        backend.capture_image(tmp_path / "page.jpg", _config(1))

    assert "not connected" in str(exc.value)


@pytest.mark.unit
def test_a_capture_logs_what_it_cost(monkeypatch, tmp_path, caplog):
    body = Body(serial="AAA111", card=EVEN_CARD, image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(body))
    backend.list_devices()

    with caplog.at_level("INFO", logger="test-chdk"):
        backend.capture_image(tmp_path / "page.jpg", _config())

    line = "\n".join(record.getMessage() for record in caplog.records)
    assert f"{len(JPEG)} bytes" in line
    assert "usb:001,004" in line
    assert "s from shutter to disk" in line


@pytest.mark.unit
def test_the_backend_reports_what_it_can_do(monkeypatch):
    backend = make_backend(monkeypatch, make_pychdk(Body()))

    assert backend.get_backend_name() == "chdk"
    assert backend.supports_streaming() is False
    assert backend.supports_live_adjustment() is False
    capabilities = backend.get_capabilities()
    assert capabilities["live_preview"] is True
    assert not any(
        value for key, value in capabilities.items() if key != "live_preview"
    )


@pytest.mark.unit
def test_two_bodies_with_one_identity_refuse_to_capture(monkeypatch, tmp_path):
    first = Body(bus=1, address=4, serial=None,
                 card=b"EVEN\nid=aaaaaaaaaaaa\n", image=JPEG)
    second = Body(bus=1, address=7, serial=None,
                  card=b"ODD\nid=aaaaaaaaaaaa\n", image=JPEG)
    backend = make_backend(monkeypatch, make_pychdk(first, second))
    backend.list_devices()

    for index in (0, 1):
        with pytest.raises(RuntimeError) as exc:
            backend.capture_image(tmp_path / f"page{index}.jpg", _config(index))
        assert "identity" in str(exc.value)

    assert first.shots == [] and second.shots == []
