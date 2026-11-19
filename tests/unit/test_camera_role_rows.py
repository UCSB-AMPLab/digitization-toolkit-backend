"""The role-reporting fields on each backend's device rows.

Every row carries the USB socket its body is plugged into, the body's serial,
and a guess at its role taken from the mechanism that places it today: the
card file for CHDK, the bindings pin for gphoto2. The guess is shown to the
operator and nothing acts on it, so these tests also check that adding the
fields changes no index, no pin and no claim.

Version: 0.1.0-beta
"""

import pytest

import capture.backends.gphoto2_backend as gb

from .chdk_fakes import Body, make_backend, make_pychdk
from tests.unit.test_gphoto2_identity import (
    MODEL, PORT_A, PORT_B, PORT_S1, SERIAL_A, SERIAL_B, _backend_with,
)


EVEN_CARD = b"EVEN\nid=aaaaaaaaaaaa\n"
ODD_CARD = b"ODD\nid=bbbbbbbbbbbb\n"

ROLE_KEYS = ("usb_port", "serial", "role_guess", "role_guess_source")


def _rows_by_index(rows):
    return {row["index"]: row for row in rows}


def _sysfs_device(root, name, busnum, devnum):
    path = root / name
    path.mkdir(parents=True)
    (path / "busnum").write_text(f"{busnum}\n")
    (path / "devnum").write_text(f"{devnum}\n")


# ----------------------------------------------------------------------
# CHDK
# ----------------------------------------------------------------------

@pytest.mark.unit
def test_chdk_rows_carry_the_socket_and_the_card_parity_as_a_guess(monkeypatch):
    odd = Body(bus=1, address=4, serial="AAA111", card=ODD_CARD, port_numbers=(1, 3))
    even = Body(bus=1, address=7, serial="BBB222", card=EVEN_CARD, port_numbers=(1, 2))
    backend = make_backend(monkeypatch, make_pychdk(odd, even))

    rows = _rows_by_index(backend.list_devices())

    assert rows[0]["usb_port"] == "1-1.2"
    assert rows[0]["serial"] == "BBB222"
    assert rows[0]["role_guess"] == "left"
    assert rows[0]["role_guess_source"] == "own_txt"
    assert rows[1]["usb_port"] == "1-1.3"
    assert rows[1]["serial"] == "AAA111"
    assert rows[1]["role_guess"] == "right"
    assert rows[1]["role_guess_source"] == "own_txt"


@pytest.mark.unit
def test_a_chdk_body_with_no_parity_and_no_ports_has_no_guess(monkeypatch):
    body = Body(serial=None, card=None)
    backend = make_backend(monkeypatch, make_pychdk(body))

    row = backend.list_devices()[0]

    assert row["usb_port"] is None
    assert row["serial"] is None
    assert row["role_guess"] is None
    assert row["role_guess_source"] is None


@pytest.mark.unit
def test_reading_the_socket_asks_the_chdk_body_nothing(monkeypatch):
    """The socket comes from pyusb's own record, not from a CHDK transaction."""
    with_ports = Body(bus=1, address=4, card=EVEN_CARD, port_numbers=(2,))
    without = Body(bus=1, address=4, card=EVEN_CARD)
    rows = []
    for body in (with_ports, without):
        backend = make_backend(monkeypatch, make_pychdk(body))
        rows.append(backend.list_devices()[0])

    assert rows[0]["usb_port"] == "1-2"
    assert with_ports.events == without.events
    assert with_ports.lua_calls == without.lua_calls
    assert with_ports.version_calls == without.version_calls
    assert with_ports.mode_switches == without.mode_switches
    assert with_ports.opens == without.opens == 1


@pytest.mark.unit
def test_the_chdk_row_keeps_its_existing_fields_and_order(monkeypatch):
    body = Body(serial="AAA111", card=EVEN_CARD, port_numbers=(1,))
    backend = make_backend(monkeypatch, make_pychdk(body))

    row = backend.list_devices()[0]

    existing = [
        "index", "model", "hardware_id", "serial", "location", "port",
        "has_aperture_control", "supports_zoom", "zoom_steps", "zoom_step",
        "side", "provisional", "identity_ambiguous", "error",
    ]
    keys = list(row)
    assert keys[:len(existing)] == existing
    for key in ROLE_KEYS:
        assert key in row
    assert row["side"] == "even"
    assert row["error"] is None


# ----------------------------------------------------------------------
# gphoto2
# ----------------------------------------------------------------------

@pytest.fixture
def projects_root(monkeypatch, tmp_path):
    """An isolated projects root, so no bindings file carries over."""
    from app.core.config import settings

    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    return root


@pytest.fixture
def sysfs(monkeypatch, tmp_path):
    root = tmp_path / "sysfs"
    root.mkdir()
    # PORT_A is usb:001,004 and PORT_B is usb:001,005.
    _sysfs_device(root, "1-1.2", 1, 4)
    _sysfs_device(root, "1-1.3", 1, 5)
    monkeypatch.setattr(gb, "_SYSFS_USB_DEVICES", str(root))
    return root


@pytest.mark.unit
def test_gphoto2_rows_carry_the_socket_and_the_pin_as_a_guess(monkeypatch, projects_root, sysfs):
    backend, *_ = _backend_with(monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)])

    rows = _rows_by_index(backend.list_devices())

    assert backend._pins == {0: SERIAL_A, 1: SERIAL_B}
    assert rows[0]["usb_port"] == "1-1.2"
    assert rows[0]["serial"] == SERIAL_A
    assert rows[0]["role_guess"] == "left"
    assert rows[0]["role_guess_source"] == "gphoto2_pin"
    assert rows[1]["usb_port"] == "1-1.3"
    assert rows[1]["serial"] == SERIAL_B
    assert rows[1]["role_guess"] == "right"
    assert rows[1]["role_guess_source"] == "gphoto2_pin"


@pytest.mark.unit
def test_an_unpinned_gphoto2_body_has_no_guess(monkeypatch, projects_root, sysfs):
    """A body that answers no serial is never pinned, so nothing guesses its role."""
    backend, *_ = _backend_with(monkeypatch, [(MODEL, PORT_S1)])

    row = backend.list_devices()[0]

    assert backend._pins == {}
    assert row["serial"] is None
    assert row["usb_port"] is None  # PORT_S1 is not in the sysfs tree
    assert row["role_guess"] is None
    assert row["role_guess_source"] is None


@pytest.mark.unit
def test_gphoto2_rows_without_sysfs_have_no_socket(monkeypatch, projects_root, tmp_path):
    monkeypatch.setattr(gb, "_SYSFS_USB_DEVICES", str(tmp_path / "absent"))
    backend, *_ = _backend_with(monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)])

    rows = _rows_by_index(backend.list_devices())

    assert rows[0]["usb_port"] is None
    assert rows[1]["usb_port"] is None
    assert rows[0]["role_guess"] == "left"


@pytest.mark.unit
def test_resolving_the_socket_makes_no_gphoto2_claim(monkeypatch, projects_root, sysfs, tmp_path):
    """The same rig claims the bodies the same number of times with or without sysfs."""
    backend, claims_with, *_ = _backend_with(monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)])
    with_rows = backend.list_devices()

    monkeypatch.setattr(gb, "_SYSFS_USB_DEVICES", str(tmp_path / "absent"))
    (projects_root / "camera-bindings.json").unlink(missing_ok=True)
    backend, claims_without, *_ = _backend_with(monkeypatch, [(MODEL, PORT_A), (MODEL, PORT_B)])
    without_rows = backend.list_devices()

    assert claims_with.init_calls == claims_without.init_calls
    assert claims_with.init_by_port == claims_without.init_by_port
    strip = lambda rows: [
        {k: v for k, v in row.items() if k != "usb_port"} for row in rows
    ]
    assert strip(with_rows) == strip(without_rows)


@pytest.mark.unit
def test_the_gphoto2_row_keeps_its_existing_fields_and_order(monkeypatch, projects_root, sysfs):
    backend, *_ = _backend_with(monkeypatch, [(MODEL, PORT_A)])

    row = backend.list_devices()[0]

    existing = [
        "index", "model", "hardware_id", "serial", "location", "port",
        "has_aperture_control", "supports_zoom",
    ]
    assert list(row)[:len(existing)] == existing
    for key in ROLE_KEYS:
        assert key in row
    assert row["port"] == PORT_A
    assert row["hardware_id"] == f"canoneosrebelt7_{SERIAL_A}"
