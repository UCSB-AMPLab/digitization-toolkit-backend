"""Enumerating CHDK bodies: which index each one takes, and who it is.

A Canon compact under CHDK has no left or right. What it has is a page
parity, written on its own card as A/OWN.TXT: ODD or EVEN says which pages
that body shoots. The mapping is fixed and direction-agnostic - EVEN is index
0, ODD is index 1 - and which parity the operator sees on the left is the
kiosk's swap toggle, not this backend's business.

Identity is the other half. pyusb reads a serial from some bodies and not
others, so a card may carry a second line, id=<hex>, and a body with neither
has no identity to register: it is listed and previewable, but it may not
capture until the side route gives it one.

Everything here runs against the fake pychdk in chdk_fakes.py; no USB.
"""

import pytest

from .chdk_fakes import Body, PTPError, make_backend, make_pychdk


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"


def _rows_by_index(rows):
    return {row["index"]: row for row in rows}


@pytest.mark.unit
def test_parity_decides_the_index_not_the_usb_order(monkeypatch):
    """The ODD body enumerates first on the bus and still lands at index 1."""
    odd = Body(bus=1, address=4, serial="AAA111", card=ODD_CARD)
    even = Body(bus=1, address=7, serial="BBB222", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(odd, even))

    rows = _rows_by_index(backend.list_devices())

    assert rows[0]["serial"] == "BBB222"
    assert rows[0]["side"] == "even"
    assert rows[1]["serial"] == "AAA111"
    assert rows[1]["side"] == "odd"


@pytest.mark.unit
def test_a_serial_makes_the_hardware_id(monkeypatch):
    body = Body(serial="AAA111", product="Canon PowerShot A2500", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))

    row = backend.list_devices()[0]

    assert row["hardware_id"] == "canonpowershota2500_AAA111"
    assert row["model"] == "Canon PowerShot A2500"
    assert row["serial"] == "AAA111"
    assert row["provisional"] is False
    assert row["error"] is None
    assert row["has_aperture_control"] is False
    assert row["supports_zoom"] is False
    assert row["location"] == "USB usb:001,004"


@pytest.mark.unit
def test_the_card_id_stands_in_when_usb_reads_no_serial(monkeypatch):
    body = Body(serial=None, product="Canon PowerShot A2500", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))

    row = backend.list_devices()[0]

    assert row["serial"] is None
    assert row["hardware_id"] == "canonpowershota2500_aaaaaaaaaaaa"
    assert row["provisional"] is False


@pytest.mark.unit
def test_an_uppercase_card_id_is_read_lowercased(monkeypatch):
    body = Body(serial=None, card=b"EVEN\nID=AAAAAAAAAAAA\n")
    backend = make_backend(monkeypatch, make_pychdk(body))

    assert backend.list_devices()[0]["hardware_id"].endswith("_aaaaaaaaaaaa")


@pytest.mark.unit
def test_a_body_with_no_identity_at_all_is_listed_but_refused(monkeypatch):
    body = Body(serial=None, card=b"EVEN\n")
    backend = make_backend(monkeypatch, make_pychdk(body))

    row = backend.list_devices()[0]

    assert row["provisional"] is True
    assert row["side"] == "even"
    assert row["index"] == 0
    assert "/side/" in row["error"]
    assert row["hardware_id"], "a listed body still needs an id the API can carry"


@pytest.mark.unit
def test_a_missing_side_file_leaves_the_parity_unset(monkeypatch):
    """A card with no OWN.TXT answers PTP 0x2002; that is absence, not failure."""
    one = Body(bus=1, address=4, serial="AAA111", card=None)
    two = Body(bus=1, address=7, serial="BBB222", card=None)
    backend = make_backend(monkeypatch, make_pychdk(one, two))

    rows = _rows_by_index(backend.list_devices())

    assert rows[0]["serial"] == "AAA111"
    assert rows[1]["serial"] == "BBB222"
    assert all(row["side"] is None for row in rows.values())
    assert one.closes == 0 and two.closes == 0


@pytest.mark.unit
def test_rubbish_on_the_card_reads_as_no_parity_and_no_id(monkeypatch):
    body = Body(serial="AAA111", card=b"\x00\x01 nonsense \xff\n")
    backend = make_backend(monkeypatch, make_pychdk(body))

    row = backend.list_devices()[0]

    assert row["side"] is None
    assert row["hardware_id"] == "canonpowershota2500_AAA111"


@pytest.mark.unit
def test_a_read_failure_that_is_not_absence_evicts_the_body(monkeypatch):
    """Anything but the general-error code means the body is unwell, not silent."""
    good = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    bad = Body(
        bus=1, address=7, serial="BBB222",
        download_error=PTPError(0x2003),
    )
    backend = make_backend(monkeypatch, make_pychdk(good, bad))

    rows = backend.list_devices()

    assert [row["serial"] for row in rows] == ["AAA111"]
    assert bad.closes == 1, "the evicted body was left open"


@pytest.mark.unit
def test_two_bodies_claiming_one_parity_keep_their_usb_indices(monkeypatch):
    first = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    second = Body(bus=1, address=7, serial="BBB222", card=b"EVEN\nid=cccccccccccc\n")
    backend = make_backend(monkeypatch, make_pychdk(first, second))

    rows = _rows_by_index(backend.list_devices())

    assert rows[0]["serial"] == "AAA111"
    assert rows[1]["serial"] == "BBB222"
    for row in rows.values():
        assert row["error"] is not None
        assert "even" in row["error"]
        assert "/side/" in row["error"]


@pytest.mark.unit
def test_no_parity_anywhere_falls_back_to_usb_order(monkeypatch):
    first = Body(bus=1, address=4, serial="AAA111", card=b"id=aaaaaaaaaaaa\n")
    second = Body(bus=1, address=7, serial="BBB222", card=b"id=bbbbbbbbbbbb\n")
    backend = make_backend(monkeypatch, make_pychdk(first, second))

    rows = _rows_by_index(backend.list_devices())

    assert rows[0]["serial"] == "AAA111"
    assert rows[1]["serial"] == "BBB222"
    assert all(row["side"] is None for row in rows.values())
    assert all(row["error"] is None for row in rows.values())


@pytest.mark.unit
def test_a_body_without_a_parity_takes_the_index_the_other_left_free(monkeypatch):
    plain = Body(bus=1, address=4, serial="AAA111", card=None)
    odd = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD)
    backend = make_backend(monkeypatch, make_pychdk(plain, odd))

    rows = _rows_by_index(backend.list_devices())

    assert rows[1]["serial"] == "BBB222", "the ODD body must keep index 1"
    assert rows[0]["serial"] == "AAA111"
    assert rows[0]["side"] is None


@pytest.mark.unit
def test_a_body_is_opened_once_across_enumerations(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))

    backend.list_devices()
    backend.list_devices()

    assert body.opens == 1, "enumeration re-opened a body it already had"


@pytest.mark.unit
def test_a_rescan_reads_the_card_again(monkeypatch):
    """A card rewritten between runs is only seen if rescan re-reads it."""
    body = Body(serial="AAA111", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))

    assert backend.list_devices()[0]["side"] == "even"
    body.card = ODD_CARD

    assert backend.rescan()[0]["side"] == "odd"


@pytest.mark.unit
def test_a_body_that_left_the_bus_is_closed_and_dropped(monkeypatch):
    staying = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    leaving = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD)
    fake = make_pychdk(staying, leaving)
    backend = make_backend(monkeypatch, fake)

    assert len(backend.list_devices()) == 2
    fake.bodies = [staying]

    rows = backend.list_devices()

    assert [row["serial"] for row in rows] == ["AAA111"]
    assert leaving.closes == 1
    assert staying.closes == 0


@pytest.mark.unit
def test_the_lock_follows_the_body_when_its_index_moves(monkeypatch):
    """A rescan may move a body between indices; its lock must move with it."""
    one = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    two = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD)
    backend = make_backend(monkeypatch, make_pychdk(one, two))

    backend.list_devices()
    lock_of_one = backend._body_at(0).lock

    one.card = ODD_CARD
    two.card = EVEN_CARD
    rows = _rows_by_index(backend.rescan())

    assert rows[1]["serial"] == "AAA111", "the parities were not swapped"
    assert backend._body_at(1).lock is lock_of_one


@pytest.mark.unit
def test_connection_state_follows_the_open_device(monkeypatch):
    """The first question enumerates; the rest read what that found."""
    body = Body(serial="AAA111", card=EVEN_CARD)
    fake = make_pychdk(body)
    backend = make_backend(monkeypatch, fake)

    assert backend.is_camera_connected(0) is True
    assert fake.list_calls == 1, "the first question must enumerate"
    assert backend.is_camera_connected(0) is True
    assert fake.list_calls == 1, "every question must not re-enumerate"
    assert backend.is_camera_connected(1) is False

    body.device.close()
    assert backend.is_camera_connected(0) is False


@pytest.mark.unit
def test_cleanup_closes_every_open_body(monkeypatch):
    one = Body(bus=1, address=4, serial="AAA111", card=EVEN_CARD)
    two = Body(bus=1, address=7, serial="BBB222", card=ODD_CARD)
    backend = make_backend(monkeypatch, make_pychdk(one, two))

    backend.list_devices()
    backend.cleanup()

    assert (one.closes, two.closes) == (1, 1)

    backend.list_devices()
    assert (one.opens, two.opens) == (2, 2), "a cleaned-up backend must reopen"


@pytest.mark.unit
def test_enumeration_logs_what_the_bench_has_to_know(monkeypatch, caplog):
    body = Body(serial=None, product="Canon PowerShot A2500", card=EVEN_CARD)
    backend = make_backend(monkeypatch, make_pychdk(body))

    with caplog.at_level("INFO", logger="test-chdk"):
        backend.list_devices()

    line = "\n".join(record.getMessage() for record in caplog.records)
    assert "no usb serial" in line.lower()
    assert "Canon PowerShot A2500" in line
    assert "even" in line.lower()
    assert "canonpowershota2500_aaaaaaaaaaaa" in line
