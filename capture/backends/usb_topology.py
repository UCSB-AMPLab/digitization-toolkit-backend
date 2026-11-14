"""The USB topology path of a device: its bus, then the hub ports to its socket.

Linux names every USB device in sysfs by this path, for example "1-1.2": bus
1, port 1 of the root hub, port 2 of the hub behind it. The path names the
socket, not the device number, so it stays the same when a body
re-enumerates while its cable stays in the same socket, and it changes when
someone moves the cable.

Every function here returns the path as a string, or None when it cannot
tell: on macOS, where there is no sysfs and pyusb may report no ports; when
the device has left the bus; when the input is not in the expected form.
None of them raises, and none of them talks to the device: pyusb reads the
bus and the ports from its own enumeration record, and sysfs is a directory
listing.

Version: 0.1.0-beta
"""

import os
import re

# Where Linux lists USB devices. The functions take it as a parameter so tests
# can point them at a directory of their own.
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"

# A device directory name in sysfs: "<bus>-<port>[.<port>...]". Root hubs
# ("usb1") and interfaces ("1-1.2:1.0") do not match and are skipped.
_TOPOLOGY_RE = re.compile(r"\d+-\d+(?:\.\d+)*")

# A libgphoto2 USB port, "usb:BBB,DDD": bus number, then device number.
_GPHOTO2_PORT_RE = re.compile(r"usb:(\d{3}),(\d{3})")


def _as_number(value):
    """The value as a non-negative int, or None. A bool is not a number here."""
    if type(value) is int and value >= 0:
        return value
    return None


def from_pyusb_device(device):
    """The topology path of a pyusb device, from its `bus` and `port_numbers`.

    pyusb gives `port_numbers` as a tuple of hub ports from the root hub
    outwards, or None when the platform's libusb backend cannot say. A device
    on a root port has one port number; an empty tuple names no socket.
    """
    try:
        bus = _as_number(getattr(device, "bus", None))
        ports = getattr(device, "port_numbers", None)
        if bus is None or not ports:
            return None
        numbers = [_as_number(port) for port in ports]
        if any(number is None for number in numbers):
            return None
        return f"{bus}-" + ".".join(str(number) for number in numbers)
    except Exception:
        return None


def _read_number(path):
    try:
        with open(path, "r") as handle:
            return int(handle.read().strip())
    except Exception:
        return None


def from_bus_address(bus, address, sysfs_root=SYSFS_USB_DEVICES):
    """The topology path of the device at this bus number and device number.

    sysfs lists each device as a directory whose name is its topology path,
    and each holds `busnum` and `devnum` files. The directory whose files
    match is the device, and its name is the answer.
    """
    try:
        bus = _as_number(bus)
        address = _as_number(address)
        if bus is None or address is None:
            return None
        root = os.fspath(sysfs_root)
        for name in sorted(os.listdir(root)):
            if not _TOPOLOGY_RE.fullmatch(name):
                continue
            entry = os.path.join(root, name)
            if (
                _read_number(os.path.join(entry, "busnum")) == bus
                and _read_number(os.path.join(entry, "devnum")) == address
            ):
                return name
        return None
    except Exception:
        return None


def from_gphoto2_port(port, sysfs_root=SYSFS_USB_DEVICES):
    """The topology path for a libgphoto2 port of the form "usb:BBB,DDD"."""
    try:
        match = _GPHOTO2_PORT_RE.fullmatch(port) if isinstance(port, str) else None
        if match is None:
            return None
        return from_bus_address(
            int(match.group(1)), int(match.group(2)), sysfs_root=sysfs_root
        )
    except Exception:
        return None
