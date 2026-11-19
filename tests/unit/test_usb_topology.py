"""The USB topology path of a camera: bus, then the hub ports to its socket.

The path names the socket a cable is plugged into, so it stays the same when
a body re-enumerates onto a new device number. The helper reads it from a
pyusb device or from sysfs, and answers None wherever it cannot tell.

Version: 0.1.0-beta
"""

import types

import pytest

from capture.backends import usb_topology


def _pyusb(bus, ports):
    return types.SimpleNamespace(bus=bus, port_numbers=ports)


class _Raising:
    """A pyusb device whose attributes raise, as a backend without port support does."""

    @property
    def bus(self):
        raise NotImplementedError("no bus")

    @property
    def port_numbers(self):
        raise NotImplementedError("no ports")


def _device_dir(root, name, busnum, devnum):
    path = root / name
    path.mkdir()
    (path / "busnum").write_text(f"{busnum}\n")
    (path / "devnum").write_text(f"{devnum}\n")
    return path


@pytest.mark.unit
def test_a_pyusb_device_behind_a_hub_gives_its_chain_of_ports():
    assert usb_topology.from_pyusb_device(_pyusb(1, (1, 2))) == "1-1.2"


@pytest.mark.unit
def test_a_pyusb_device_on_a_root_port_gives_one_port():
    assert usb_topology.from_pyusb_device(_pyusb(3, (1,))) == "3-1"


@pytest.mark.unit
@pytest.mark.parametrize("device", [
    None,
    _pyusb(None, (1, 2)),
    _pyusb(1, None),
    _pyusb(1, ()),
    _pyusb("x", (1,)),
    _pyusb(1, ("a",)),
    types.SimpleNamespace(),
    _Raising(),
])
def test_a_pyusb_device_without_a_topology_gives_none(device):
    assert usb_topology.from_pyusb_device(device) is None


@pytest.mark.unit
def test_sysfs_names_the_device_with_that_bus_and_address(tmp_path):
    _device_dir(tmp_path, "usb1", 1, 1)
    _device_dir(tmp_path, "1-1", 1, 2)
    _device_dir(tmp_path, "1-1.2", 1, 5)
    _device_dir(tmp_path, "1-1.3", 1, 6)
    (tmp_path / "1-1.2:1.0").mkdir()
    _device_dir(tmp_path, "3-1", 3, 5)

    assert usb_topology.from_bus_address(1, 5, sysfs_root=tmp_path) == "1-1.2"
    assert usb_topology.from_bus_address(3, 5, sysfs_root=tmp_path) == "3-1"


@pytest.mark.unit
def test_sysfs_without_a_matching_device_gives_none(tmp_path):
    _device_dir(tmp_path, "1-1.2", 1, 5)

    assert usb_topology.from_bus_address(1, 9, sysfs_root=tmp_path) is None


@pytest.mark.unit
def test_a_root_hub_is_not_a_topology_path(tmp_path):
    _device_dir(tmp_path, "usb1", 1, 1)

    assert usb_topology.from_bus_address(1, 1, sysfs_root=tmp_path) is None


@pytest.mark.unit
def test_a_missing_sysfs_tree_gives_none(tmp_path):
    assert usb_topology.from_bus_address(1, 5, sysfs_root=tmp_path / "absent") is None


@pytest.mark.unit
def test_unreadable_sysfs_entries_are_skipped_and_nothing_raises(tmp_path):
    broken = tmp_path / "1-1.4"
    broken.mkdir()
    (broken / "busnum").write_text("not a number\n")
    (broken / "devnum").write_text("5\n")
    (tmp_path / "1-1.5").mkdir()  # no busnum or devnum at all
    (tmp_path / "stray-file").write_text("")
    _device_dir(tmp_path, "1-1.2", 1, 5)

    assert usb_topology.from_bus_address(1, 5, sysfs_root=tmp_path) == "1-1.2"


@pytest.mark.unit
@pytest.mark.parametrize("bus, address", [(None, 5), (1, None), ("x", 5)])
def test_a_bus_or_address_that_is_not_a_number_gives_none(tmp_path, bus, address):
    _device_dir(tmp_path, "1-1.2", 1, 5)

    assert usb_topology.from_bus_address(bus, address, sysfs_root=tmp_path) is None


@pytest.mark.unit
def test_a_libgphoto2_port_resolves_through_sysfs(tmp_path):
    _device_dir(tmp_path, "1-1.2", 1, 5)

    assert usb_topology.from_gphoto2_port("usb:001,005", sysfs_root=tmp_path) == "1-1.2"


@pytest.mark.unit
@pytest.mark.parametrize("port", [None, "", "ptpip:10.0.0.2", "usb:", "usb:1,5"])
def test_a_port_not_in_libgphoto2_usb_form_gives_none(tmp_path, port):
    _device_dir(tmp_path, "1-1.2", 1, 5)

    assert usb_topology.from_gphoto2_port(port, sysfs_root=tmp_path) is None
