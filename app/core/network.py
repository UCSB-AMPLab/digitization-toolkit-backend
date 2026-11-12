# Version: 0.3.4
"""The unit's network, through NetworkManager's nmcli (NEH-253).

Status, the Wi-Fi scan, joining a Wi-Fi network (open, password, or
WPA2-Enterprise) and forgetting a saved one. Every external command runs
through _command, as an argv list with no shell, with LC_ALL=C.UTF-8 in its
environment: the parsers read nmcli's English messages, and under plain C
glib may replace non-ASCII SSID bytes with `?`. SSIDs are compared exactly,
spaces included; only the line terminator is taken off nmcli's lines. The backend
runs as user `pi`, and a polkit rule installed by the superproject lets it
scan, activate and save connections; `nmcli general permissions` says
whether that rule is in place. A join it does not allow is refused before
anything runs; a scan it does not allow answers from the cached list.

Secrets. No password is ever placed in an argv: a join adds its profile
without one and hands the secret to `nmcli connection up` on stdin
(`passwd-file /dev/stdin`). NetworkManager saves the secret into the
profile on `connection up`, after a failed activation as well as after a
join; a failed join's profile is deleted, so its secret goes with it. The
802.1X identity is the exception to "nothing secret in argv": it is in the
`connection add` argv as well as on stdin, because NetworkManager 1.42
refuses an 802.1X profile without it ("802-1x.identity: property is
missing"). A join can fail with "Secrets were required, but not provided"
when another `nmcli` client of the same user runs an activation at the same
moment (seen once on the unit); it is reported as `failed`.
Even so, no argv or stdin text is logged, and no argv, stdin, stdout or
stderr of a command is put into an exception message, a log line or a
response. The join state holds the SSID, the outcome and timestamps only.
Nothing here reads a saved secret back: no command passes
`-s`/`--show-secrets` or names a secret field.

One Wi-Fi device. The device every scan, activation and restore acts on is
settings.WIFI_DEVICE when `nmcli device status` lists it as a `wifi` row, or
else the first `wifi` row; each such command passes `ifname <device>`.

A join (join, run on a daemon thread by app.api.network) adds a profile
under a UUID the backend generates, activates it with
`nmcli --wait 45 connection up`, and meanwhile reads wpa_supplicant's
journal lines about once a second. On a wrong password nmcli only waits out
its 45 s (exit code 3) while NetworkManager keeps retrying, and only the
journal names the cause. Only lines after this join's first attempt on the
target SSID count; a certificate error or MSCHAPv2 error 691 stops the
activation at once, and a weaker failure line stops it on its second
occurrence (classify). On
success the other saved profiles for the same SSID are deleted; on every
other path the new profile is deleted, which stops NetworkManager's
retries, and the connection that was active before is re-activated unless
NetworkManager has already brought it back.

Still to be run on a real unit before this is relied on (the rest was
confirmed on the unit with simulated radios, as the service user): whether
the anchor and failure lines classify reads appear at all with the Pi's
built-in radio driver (brcmfmac; every capture so far comes from a
simulated radio); whether the wait of roughly 20 s between the local
disconnect and the first attempt exists on real hardware (on the simulated
radio it appears on joins started from a connected device, and not on all
of them); and passwd-file values with trailing tabs (802.1X only, since a
WPA2/WPA3 passphrase cannot hold a tab).

This module imports nothing from FastAPI.
"""

import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from app.core import config

logger = logging.getLogger(__name__)

# Seams for the tests: the process factory, the two clocks and the UUID source.
_spawn = subprocess.Popen
_monotonic = time.monotonic
_wall_clock = time.time
_uuid4 = uuid.uuid4

NOT_AVAILABLE = "Network settings are not available on this device."

_READ_TIMEOUT = 15.0
# A fresh scan (`--rescan yes`) takes about 10 s on a unit.
_SCAN_TIMEOUT = 20.0
_WRITE_TIMEOUT = 15.0
_JOURNAL_TIMEOUT = 5.0
_UP_WAIT = 45
_RESTORE_WAIT = 30
# nmcli ends itself after --wait; the runner's own limit is a margin above it.
_UP_TIMEOUT = _UP_WAIT + 15.0
_RESTORE_TIMEOUT = _RESTORE_WAIT + 15.0
_TICK_SECONDS = 1.0
_HALT_GRACE = 5.0
# nmcli's exit code for "Timeout expired" (nmcli(1), EXIT STATUS).
_NMCLI_TIMEOUT_EXIT = 3

WIFI_TYPE = "802-11-wireless"
_INTERFACE_TYPES = ("ethernet", "wifi")
# Docker's veth devices are `ethernet` rows of `nmcli device status`.
_HIDDEN_PREFIXES = ("veth",)
_LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
# Labels of letters, digits and inner hyphens; at least two; the last not all digits.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")
# A secret or identity with one of these would end its passwd-file line early.
_LINE_BREAKS = ("\n", "\r", "\x00")

# The polkit actions of `nmcli general permissions`, by status key.
PERMISSIONS = {
    "scan": "org.freedesktop.NetworkManager.wifi.scan",
    "control": "org.freedesktop.NetworkManager.network-control",
    "modify": "org.freedesktop.NetworkManager.settings.modify.system",
}

REFUSAL_MESSAGES = {
    "ssid_invalid": "The network name must be between 1 and 32 bytes long, without line breaks.",
    "password_required": "This network needs a password.",
    "password_length": "A Wi-Fi password must be between 8 and 63 characters long.",
    "password_invalid": "The password cannot contain a line break or another control character.",
    "identity_invalid": "The user name cannot contain a line break.",
    "anonymous_identity_invalid": "The anonymous identity cannot contain a line break.",
    "method_required": "This network needs an authentication method (PEAP or TTLS).",
    "identity_required": "This network needs a user name.",
    "server_name_required": "This network needs the name of its authentication server.",
    "server_name_invalid": "The server name must be a host name such as radius.example.edu.",
    "inner_method_invalid": "PEAP can only be used with MSCHAPv2.",
    "no_wifi_device": "This unit has no Wi-Fi device.",
    "join_in_progress": "The unit is already joining a network.",
    "connection_active": "The unit is using this network now.",
    "not_permitted": "This unit does not allow the backend to change its network settings.",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NetworkUnavailable(Exception):
    """A command could not be run, did not finish, or a read command failed.

    The message never carries the command's argv, stdout or stderr."""


class NetworkRefused(Exception):
    """A request refused before anything ran; `codes` index REFUSAL_MESSAGES."""

    def __init__(self, codes: list[str]):
        super().__init__(", ".join(codes))
        self.codes = list(codes)

    def detail(self) -> dict:
        problems = [{"code": c, "message": REFUSAL_MESSAGES[c]} for c in self.codes]
        return {"message": problems[0]["message"] if len(problems) == 1
                else "The request cannot be carried out.", "problems": problems}


class SavedNotFound(Exception):
    """No saved Wi-Fi connection has this UUID."""


# ---------------------------------------------------------------------------
# The command runner
# ---------------------------------------------------------------------------

@dataclass
class _Result:
    returncode: Optional[int]
    stdout: str
    stderr: str
    stopped: bool = False


def _halt(child) -> tuple[str, str]:
    """Stop a child: terminate, then kill if it has not ended within the grace period."""
    child.terminate()
    try:
        return child.communicate(timeout=_HALT_GRACE)
    except subprocess.TimeoutExpired:
        pass
    child.kill()
    return child.communicate()


def _child_env() -> dict:
    """The backend's environment with LC_ALL=C.UTF-8: English messages, UTF-8 output."""
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    return env


def _command(argv: list[str], *, timeout: float, on_tick: Optional[Callable[[], bool]] = None,
             input_text: Optional[str] = None) -> _Result:
    """Run one command; the only place in this module that starts a process.

    argv is a list and no shell is used; stdout and stderr are captured as
    text; the environment is the backend's with LC_ALL=C.UTF-8. stdin is
    /dev/null, or, when `input_text` is given, a pipe that receives it and is
    then closed (the only way a secret reaches a command). When `on_tick` is
    given it is called about once a second while the command runs, and a
    true answer stops the command (the result then has stopped=True). A
    command that cannot be started, that outlives `timeout`, or whose output
    is not UTF-8 raises NetworkUnavailable. Neither argv nor input_text is logged or put into
    an exception, and these raises happen outside any `except` block so the
    new exception carries no context (subprocess.TimeoutExpired holds the
    argv).
    """
    failure = None
    stdin = subprocess.PIPE if input_text is not None else subprocess.DEVNULL
    try:
        child = _spawn(argv, stdin=stdin, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, env=_child_env())
    except (OSError, ValueError):
        # ValueError: an argv value with an embedded NUL.
        failure = "A network command could not be started."
    if failure is not None:
        raise NetworkUnavailable(failure)

    # communicate() takes the input once; later calls carry on writing it.
    pending = input_text
    deadline = _monotonic() + timeout
    finished = False
    try:
        while True:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                _halt(child)
                finished = True
                failure = f"A network command did not finish within {timeout:g} s."
                break
            wait = min(remaining, _TICK_SECONDS) if on_tick is not None else remaining
            try:
                given, pending = pending, None
                stdout, stderr = child.communicate(input=given, timeout=wait)
            except UnicodeDecodeError:
                failure = "A network command answered with text that is not UTF-8."
                break
            except subprocess.TimeoutExpired:
                if on_tick is not None and on_tick():
                    stdout, stderr = _halt(child)
                    finished = True
                    return _Result(child.returncode, stdout or "", stderr or "", stopped=True)
                continue
            finished = True
            return _Result(child.returncode, stdout or "", stderr or "")
    finally:
        if not finished:
            # An exception from on_tick or from the child: never leave it running.
            try:
                _halt(child)
            except Exception:
                pass
    raise NetworkUnavailable(failure)


def _read(argv: list[str], timeout: float = _READ_TIMEOUT) -> str:
    """stdout of a read command; a non-zero exit is NetworkUnavailable."""
    result = _command(argv, timeout=timeout)
    if result.returncode != 0:
        raise NetworkUnavailable("NetworkManager did not answer.")
    return result.stdout


def available() -> bool:
    return shutil.which("nmcli") is not None


# ---------------------------------------------------------------------------
# Terse output
# ---------------------------------------------------------------------------

def split_terse(line: str, maxsplit: int = -1) -> list[str]:
    """Split one line of tabular `nmcli -t` output on unescaped colons.

    In tabular output (`device status`, `connection show`, `device wifi
    list`) nmcli writes `:` inside a value as `\\:` and `\\` as `\\\\`;
    every field is unescaped. With maxsplit, at most that many splits are
    made and the rest, unescaped too, is the last field. Multiline output
    (`connection show <id>`, `device show`) is not escaped: parse_fields
    reads it."""
    fields, current, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            current.append(line[i + 1])
            i += 2
            continue
        if c == ":" and (maxsplit < 0 or len(fields) < maxsplit):
            fields.append("".join(current))
            current = []
        else:
            current.append(c)
        i += 1
    fields.append("".join(current))
    return fields


def _lines(text: str) -> list[str]:
    """nmcli's lines with only the terminator taken off. str.splitlines()
    would also split inside an SSID at characters such as U+2028."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # nmcli ends lines with \n alone; a \r before it belongs to the value.
    return lines


def parse_fields(text: str) -> dict[str, list[str]]:
    """`NAME:value` lines (multiline terse output) as {NAME: [values]}.

    nmcli does not escape multiline output (the unit printed
    `802-11-wireless.ssid:cap\\bs` for the SSID `cap\\bs`), so each line is
    split at its first colon and the value taken raw. `IP4.ADDRESS[1]`,
    `IP4.ADDRESS[2]`, ... are collected under `IP4.ADDRESS` in order."""
    fields: dict[str, list[str]] = {}
    for line in _lines(text):
        if not line:
            continue
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        name = re.sub(r"\[\d+\]$", "", parts[0])
        fields.setdefault(name, []).append(parts[1])
    return fields


def _value(raw: Optional[str]) -> Optional[str]:
    """None for nmcli's empty values ("" and "--")."""
    if raw is None:
        return None
    raw = raw.strip()
    return None if raw in ("", "--") else raw


def _values(fields: dict, name: str) -> list[str]:
    return [v for v in (_value(x) for x in fields.get(name, [])) if v is not None]


def _ssid_value(fields: dict, name: str) -> str:
    """An SSID field exactly as nmcli wrote it: spaces are part of an SSID.

    "--" is kept: `connection show uuid` prints no 802-11-wireless.ssid line
    for a profile that is not Wi-Fi and always prints the SSID of one that
    is, so "--" there is a network's name, not a placeholder."""
    return (fields.get(name) or [""])[0]


# ---------------------------------------------------------------------------
# Devices and status
# ---------------------------------------------------------------------------

def _devices() -> list[dict]:
    """Rows of `nmcli device status`, every type."""
    out = _read(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    rows = []
    for line in _lines(out):
        if not line.strip():
            continue
        parts = split_terse(line, maxsplit=3)
        if len(parts) < 4:
            continue
        rows.append({"device": parts[0], "type": parts[1], "state": parts[2], "connection": _value(parts[3])})
    return rows


def _wifi_device_from(rows: list[dict]) -> Optional[str]:
    configured = config.settings.WIFI_DEVICE
    wifi = [row["device"] for row in rows if row["type"] == "wifi"]
    if configured:
        # A configured device that NetworkManager does not list is no device.
        return configured if configured in wifi else None
    return wifi[0] if wifi else None


def wifi_device() -> Optional[str]:
    """settings.WIFI_DEVICE when `nmcli device status` lists it as a `wifi`
    row, else (when unset) the first `wifi` row; None when there is none."""
    return _wifi_device_from(_devices())


def _listed(row: dict) -> bool:
    """Ethernet and Wi-Fi rows, without Docker's veth devices."""
    return row["type"] in _INTERFACE_TYPES and not row["device"].startswith(_HIDDEN_PREFIXES)


def permissions() -> dict:
    """{scan, control, modify}: whether `nmcli general permissions` answers
    `yes` for wifi.scan, network-control and settings.modify.system. `auth`,
    `no` and a missing line are all false. NetworkUnavailable when the read
    fails."""
    values = {}
    for line in _lines(_read(["nmcli", "-t", "general", "permissions"])):
        parts = split_terse(line.strip(), maxsplit=1)
        if len(parts) == 2:
            values[parts[0]] = parts[1].strip().lower()
    return {key: values.get(action) == "yes" for key, action in PERMISSIONS.items()}


def _state(raw: str) -> str:
    """nmcli's device state ("connected (externally)", "connecting (getting
    IP configuration)", ...) as one of five values."""
    raw = raw.strip().lower()
    for name in ("connected", "connecting", "disconnected", "unavailable", "unmanaged"):
        if raw.startswith(name):
            return name
    if raw.startswith("deactivating"):
        return "disconnected"
    return "unavailable"


def _wifi_rows(device: str, rescan: str) -> list[dict]:
    """The Wi-Fi list on `device`; `rescan` is nmcli's "yes", "auto" or "no"."""
    timeout = _SCAN_TIMEOUT if rescan != "no" else _READ_TIMEOUT
    out = _read(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                 "ifname", device, "--rescan", rescan], timeout=timeout)
    rows = []
    for line in _lines(out):
        if not line:
            continue
        parts = split_terse(line, maxsplit=3)
        if len(parts) < 4:
            continue
        try:
            signal = int(parts[2].strip())
        except ValueError:
            signal = 0
        rows.append({"inUse": parts[0].strip() == "*", "ssid": parts[1], "signal": signal,
                     "securityRaw": parts[3].strip()})
    return rows


def _security_of_scan(raw: str) -> str:
    if "802.1X" in raw:
        return "enterprise"
    if raw in ("", "--"):
        return "open"
    return "password"


def _interface(row: dict) -> dict:
    device = row["device"]
    fields = parse_fields(_read(["nmcli", "-t", "-f",
                                 "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS",
                                 "device", "show", device]))
    state = _state(row["state"])
    ssid = signal = None
    if row["type"] == "wifi" and state == "connected":
        for wifi in _wifi_rows(device, "no"):
            if wifi["inUse"]:
                ssid, signal = wifi["ssid"], wifi["signal"]
                break
    # The IPv4 gateway only; DNS servers IPv4 first, then IPv6.
    gateway = _values(fields, "IP4.GATEWAY")
    return {
        "device": device, "type": row["type"], "state": state, "connection": row["connection"],
        "mac": (_values(fields, "GENERAL.HWADDR") or [None])[0],
        "ipv4": _values(fields, "IP4.ADDRESS"), "ipv6": _values(fields, "IP6.ADDRESS"),
        "gateway": gateway[0] if gateway else None,
        "dns": _values(fields, "IP4.DNS") + _values(fields, "IP6.DNS"),
        "ssid": ssid, "signal": signal,
    }


def _is_link_local(address: str) -> bool:
    try:
        return ipaddress.ip_interface(address).ip in _LINK_LOCAL
    except ValueError:
        return False


def _problems(interfaces: list[dict]) -> list[dict]:
    connected = [i for i in interfaces if i["state"] == "connected"]
    if not connected:
        return [{"code": "no_connection", "device": None}]
    problems = []
    for i in connected:
        device = i["device"]
        if not i["ipv4"]:
            problems.append({"code": "no_address", "device": device})
        elif all(_is_link_local(a) for a in i["ipv4"]):
            # A link-local address means DHCP failed; gateway and DNS add nothing.
            problems.append({"code": "link_local_only", "device": device})
            continue
        if not i["gateway"]:
            problems.append({"code": "no_gateway", "device": device})
        if not i["dns"]:
            problems.append({"code": "no_dns", "device": device})
    return problems


def status() -> dict:
    """GET /network/status: {hostname, wifiDevice, permissions, interfaces, problems}.

    Interfaces are the Ethernet and Wi-Fi rows of `nmcli device status`
    (Docker's veth devices left out); each connected Wi-Fi device has the
    SSID and signal of its own in-use row of the cached Wi-Fi list.
    `gateway` is the IPv4 gateway only; `dns` lists the IPv4 servers, then
    the IPv6 ones. NetworkManager's own connectivity value is not reported:
    it reads `limited` on units whose internet works. Any read that fails
    raises NetworkUnavailable (the route answers 503)."""
    rows = _devices()
    interfaces = [_interface(r) for r in rows if _listed(r)]
    return {
        "hostname": socket.gethostname(),
        "wifiDevice": _wifi_device_from(rows),
        "permissions": permissions(),
        "interfaces": interfaces,
        "problems": _problems(interfaces),
    }


# ---------------------------------------------------------------------------
# Saved connections and the scan
# ---------------------------------------------------------------------------

def _security_of_key_mgmt(key_mgmt: Optional[str]) -> str:
    if key_mgmt == "wpa-eap":
        return "enterprise"
    if not key_mgmt:
        return "open"
    return "password"


def _vanished(result: "_Result", uuid: str) -> bool:
    """nmcli's answer for a profile that no longer exists (rc 10, as on the unit)."""
    return result.returncode == 10 and f"{uuid} - no such connection profile" in (result.stderr or "")


def _wifi_details(uuid: str) -> Optional[tuple[str, str]]:
    """(ssid, security) of one saved Wi-Fi connection; None when it has vanished. Names no secret field.

    For a fraction of a second after an active profile is deleted, the
    listing still shows it while the detail read answers "no such
    connection profile"; such a profile is gone, not an error. Any other
    failure raises NetworkUnavailable."""
    result = _command(["nmcli", "-t", "-f", "802-11-wireless.ssid,802-11-wireless-security.key-mgmt",
                       "connection", "show", "uuid", uuid], timeout=_READ_TIMEOUT)
    if result.returncode == 0:
        fields = parse_fields(result.stdout)
        ssid = _ssid_value(fields, "802-11-wireless.ssid")
        return ssid, _security_of_key_mgmt((_values(fields, "802-11-wireless-security.key-mgmt") or [None])[0])
    if _vanished(result, uuid):
        return None
    # A profile without a security setting may refuse the second field.
    result = _command(["nmcli", "-t", "-f", "802-11-wireless.ssid", "connection", "show", "uuid", uuid],
                      timeout=_READ_TIMEOUT)
    if result.returncode == 0:
        return _ssid_value(parse_fields(result.stdout), "802-11-wireless.ssid"), "open"
    if _vanished(result, uuid):
        return None
    raise NetworkUnavailable("NetworkManager did not answer.")


def saved_networks() -> list[dict]:
    """Saved connections of type 802-11-wireless, as {uuid, name, ssid, security, active}."""
    out = _read(["nmcli", "-t", "-f", "NAME,UUID,TYPE,ACTIVE", "connection", "show"])
    saved = []
    for line in _lines(out):
        if not line.strip():
            continue
        parts = split_terse(line, maxsplit=3)
        if len(parts) < 4 or parts[2] != WIFI_TYPE:
            continue
        details = _wifi_details(parts[1])
        if details is None:
            continue
        ssid, security = details
        saved.append({"uuid": parts[1], "name": parts[0], "ssid": ssid, "security": security,
                      "active": parts[3].strip().lower() == "yes"})
    return saved


def scan(rescan: bool = False) -> dict:
    """GET /network/wifi: {networks, saved, rescanned}.

    With the wifi.scan permission: `--rescan auto` by default, which answers
    at once from a recent scan, or with `rescan` `--rescan yes`, a fresh scan
    of about 10 s. Without it the scan is not refused: `--rescan no` lists
    the cached networks whatever was asked. `rescanned` is true only when
    `--rescan yes` was requested from NetworkManager for this answer.
    Refused with no_wifi_device only."""
    device = wifi_device()
    if device is None:
        raise NetworkRefused(["no_wifi_device"])
    if not permissions()["scan"]:
        mode = "no"
    else:
        mode = "yes" if rescan else "auto"
    rows = _wifi_rows(device, mode)
    saved = saved_networks()
    saved_ssids = {s["ssid"] for s in saved}
    best: dict[str, dict] = {}
    for row in rows:
        if not row["ssid"]:
            continue
        current = best.get(row["ssid"])
        in_use = row["inUse"] or (current is not None and current["inUse"])
        if current is None or row["signal"] > current["signal"]:
            best[row["ssid"]] = {"ssid": row["ssid"], "signal": row["signal"],
                                 "security": _security_of_scan(row["securityRaw"]),
                                 "inUse": in_use, "saved": row["ssid"] in saved_ssids}
        else:
            current["inUse"] = in_use
    networks = sorted(best.values(), key=lambda n: (not n["inUse"], -n["signal"], n["ssid"]))
    return {"networks": networks, "saved": saved, "rescanned": mode == "yes"}


def forget(uuid: str, *, force: bool) -> str:
    """Delete one saved Wi-Fi connection; returns its SSID.

    SavedNotFound when no saved Wi-Fi connection has this UUID;
    NetworkRefused(connection_active) when it is active and force is false;
    NetworkRefused(not_permitted) when polkit refuses the deletion; any
    other failure is NetworkUnavailable."""
    uuid = uuid.lower()
    match = next((s for s in saved_networks() if s["uuid"].lower() == uuid), None)
    if match is None:
        raise SavedNotFound(uuid)
    if match["active"] and not force:
        raise NetworkRefused(["connection_active"])
    result = _command(["nmcli", "connection", "delete", "uuid", match["uuid"]], timeout=_WRITE_TIMEOUT)
    if result.returncode != 0:
        if _not_permitted(result.stderr):
            raise NetworkRefused(["not_permitted"])
        raise NetworkUnavailable("NetworkManager did not delete the connection.")
    return match["ssid"]


# ---------------------------------------------------------------------------
# Join: validation and profile arguments
# ---------------------------------------------------------------------------

def _valid_server_name(server_name: Optional[str]) -> bool:
    if not server_name or _HOSTNAME.fullmatch(server_name) is None:
        return False
    # An all-digit last label is an IP address, not a server name.
    return not server_name.rsplit(".", 1)[1].isdigit()


def _inner_method(method: str, inner_method: Optional[str]) -> str:
    if inner_method:
        return inner_method
    return "mschapv2" if method == "peap" else "pap"


def _has_line_break(value: str) -> bool:
    return any(c in value for c in _LINE_BREAKS)


def _has_control_character(value: str) -> bool:
    """An ASCII control character (below 32, or 127). wpa_supplicant refuses
    such a WPA2/WPA3 passphrase ("Invalid passphrase character" on the unit,
    for a leading tab), so the join could never succeed. 802.1X passwords
    are not held to this: a leading tab in a PEAP password joined."""
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def join_problems(*, ssid: str, security: str, password: Optional[str], identity: Optional[str],
                  method: Optional[str], inner_method: Optional[str], server_name: Optional[str],
                  anonymous_identity: Optional[str] = None) -> list[str]:
    """The refusal codes of a join request, in a fixed order; empty when it may run."""
    problems = []
    if not ssid or len(ssid.encode("utf-8")) > 32 or _has_line_break(ssid):
        problems.append("ssid_invalid")
    if security == "password":
        if not password:
            problems.append("password_required")
        elif _has_control_character(password):
            problems.append("password_invalid")
        elif not 8 <= len(password.encode("utf-8")) <= 63:
            problems.append("password_length")
    elif security == "enterprise":
        if not password:
            problems.append("password_required")
        elif _has_line_break(password):
            problems.append("password_invalid")
        if not identity:
            problems.append("identity_required")
        elif _has_line_break(identity):
            problems.append("identity_invalid")
        if anonymous_identity and _has_line_break(anonymous_identity):
            problems.append("anonymous_identity_invalid")
        if method is None:
            problems.append("method_required")
        if not server_name:
            problems.append("server_name_required")
        elif not _valid_server_name(server_name):
            problems.append("server_name_invalid")
        if method == "peap" and inner_method not in (None, "mschapv2"):
            problems.append("inner_method_invalid")
    return problems


def enterprise_args(*, method: str, inner_method: Optional[str], identity: str,
                    anonymous_identity: Optional[str], server_name: Optional[str]) -> list[str]:
    """The `nmcli connection add` arguments of a WPA2-Enterprise profile.

    Completeness: this is the only function in the backend that emits
    `wifi-sec.key-mgmt wpa-eap`, and every list it returns ends with
    `802-1x.system-ca-certs yes 802-1x.domain-suffix-match <server_name>`.
    It has no parameter, branch or setting that leaves either out, and it
    raises ValueError, before building anything, when server_name is not a
    host name of at least two labels. So no enterprise profile is created
    without validating the server's certificate against the system CA store
    and its name against server_name.

    It takes no password, so it cannot place one in the argv; the password
    goes on stdin (_secrets_text). The identity is in the profile as well as
    on stdin: NetworkManager 1.42 refuses `connection add` for an 802.1X
    profile without `802-1x.identity` ("property is missing"), so it cannot
    travel on stdin alone.
    """
    if not _valid_server_name(server_name):
        raise ValueError("An enterprise profile needs a valid server name.")
    args = ["wifi-sec.key-mgmt", "wpa-eap",
            "802-1x.eap", method,
            "802-1x.phase2-auth", _inner_method(method, inner_method),
            "802-1x.identity", identity]
    if anonymous_identity:
        args += ["802-1x.anonymous-identity", anonymous_identity]
    args += ["802-1x.system-ca-certs", "yes",
             "802-1x.domain-suffix-match", server_name]
    return args


def _password_key_mgmt(device: str, ssid: str) -> str:
    """`sae` when the cached scan shows the SSID with WPA3 and neither WPA1 nor
    WPA2 on any row; else `wpa-psk` (also when the list cannot be read)."""
    try:
        rows = [r for r in _wifi_rows(device, "no") if r["ssid"] == ssid]
    except NetworkUnavailable:
        return "wpa-psk"
    tokens = set()
    for row in rows:
        tokens.update(row["securityRaw"].split())
    if "WPA3" in tokens and not tokens & {"WPA1", "WPA2"}:
        return "sae"
    return "wpa-psk"


def _profile_args(*, device: str, ssid: str, security: str, profile_uuid: str, identity: Optional[str],
                  method: Optional[str], inner_method: Optional[str], anonymous_identity: Optional[str],
                  server_name: Optional[str]) -> list[str]:
    """The `nmcli connection add` argv of a new profile, without any secret.

    It has no password parameter: the profile is saved without a secret and
    NetworkManager stores the one given on stdin when `connection up` runs."""
    argv = ["nmcli", "connection", "add", "type", "wifi", "con-name", ssid, "ifname", device, "ssid", ssid,
            "connection.uuid", profile_uuid]
    if security == "password":
        argv += ["wifi-sec.key-mgmt", _password_key_mgmt(device, ssid)]
    elif security == "enterprise":
        argv += enterprise_args(method=method, inner_method=inner_method, identity=identity,
                                anonymous_identity=anonymous_identity, server_name=server_name)
    elif security != "open":
        raise ValueError("Unknown security kind.")
    return argv


def _secrets_text(*, security: str, password: Optional[str], identity: Optional[str]) -> Optional[str]:
    """The passwd-file lines `nmcli connection up ... passwd-file /dev/stdin` reads; None for an open network.

    WPA2 password networks: `802-11-wireless-security.psk:<password>`. SAE
    (WPA3) networks take their password under the same key, although the
    profile's key-mgmt differs and a wrong SAE password shows in the journal
    differently (classify). Enterprise: `802-1x.identity:<identity>` and
    `802-1x.password:<password>` (nmcli asks for the identity in the file
    even when the profile holds it).

    Every value is encoded by _passwd_file_value, because nmcli's
    passwd-file reader strips unescaped whitespace at both ends of a value
    and treats a backslash as an escape.

    Completeness of "no secret in argv": the password reaches this module
    through join(), and join() passes it to this function only; this
    function's text goes to _command as input_text only, which writes it to
    the child's stdin pipe and never into argv, a log line or an exception.
    _profile_args and enterprise_args take no password parameter; the
    activation, deletion, restore, read and journal argvs are built from
    constants, the device, the SSID and UUIDs. So no code path places the
    password in an argv list. The identity is placed in exactly one argv,
    as the value of `802-1x.identity` in `connection add` (enterprise_args):
    the claim made here is "no password in any argv", not "no identity".

    A value with a line break or NUL would end its line early and could add
    another; such values raise ValueError (join_problems refuses them first).
    """
    if security == "open":
        return None
    if not password or _has_line_break(password):
        raise ValueError("The password cannot be passed to nmcli.")
    if security == "password":
        return f"802-11-wireless-security.psk:{_passwd_file_value(password)}\n"
    if security == "enterprise":
        if not identity or _has_line_break(identity):
            raise ValueError("The identity cannot be passed to nmcli.")
        return (f"802-1x.identity:{_passwd_file_value(identity)}\n"
                f"802-1x.password:{_passwd_file_value(password)}\n")
    raise ValueError("Unknown security kind.")


_EDGE_WHITESPACE = (" ", "\t")


def _passwd_file_value(value: str) -> str:
    """One value as nmcli's passwd-file reader gives it back unchanged.

    Each backslash is doubled, and each leading and each trailing space or
    tab gets a backslash before it; interior spaces and colons are left as
    they are. This is the encoding the unit accepted on NetworkManager 1.42
    (a PSK with a leading space, a trailing space, or a backslash, each
    joined and saved at its own length); the reader did not otherwise
    transform interior characters there. Unescaped, those three PSKs were
    changed and the joins failed as wrong_password.
    """
    value = value.replace("\\", "\\\\")
    lead = len(value) - len(value.lstrip("".join(_EDGE_WHITESPACE)))
    if lead == len(value):
        return "".join("\\" + c for c in value)
    trail = len(value) - len(value.rstrip("".join(_EDGE_WHITESPACE)))
    middle = value[lead:len(value) - trail]
    return ("".join("\\" + c for c in value[:lead]) + middle
            + "".join("\\" + c for c in value[len(value) - trail:]))


# ---------------------------------------------------------------------------
# Join: the journal
# ---------------------------------------------------------------------------

# The prefix of `journalctl -o short-precise` ("Nov 11 09:43:23.740977 host ident[pid]: ")
# and of `-o short-unix` ("1794419003.740977 host ident[pid]: ", whose epoch is kept).
_JOURNAL_PREFIX = re.compile(r"^[A-Z][a-z]{2} [ \d]?\d \d{2}:\d{2}:\d{2}(?:\.\d+)? \S+ [^\s:\[]+(?:\[\d+\])?: ")
_UNIX_PREFIX = re.compile(r"^(\d+(?:\.\d+)?) \S+ [^\s:\[]+(?:\[\d+\])?: ")
_PEER_CERT = re.compile(r"^CTRL-EVENT-EAP-PEER-CERT depth=0 subject='(.*)' hash=([0-9a-fA-F]+)")
_PEER_ALT = re.compile(r"^CTRL-EVENT-EAP-PEER-ALT depth=0 (.*)$")
_CERT_ERR = re.compile(r"err='(.*)'\s*$")


def _message(line: str) -> tuple[Optional[float], str]:
    """(epoch, message) of one journal line; the epoch only for `-o short-unix` lines."""
    line = line.rstrip("\n")
    unix = _UNIX_PREFIX.match(line)
    if unix:
        return float(unix.group(1)), line[unix.end():]
    return None, _JOURNAL_PREFIX.sub("", line, count=1)


def _this_join(lines: list[str], device: str, since: Optional[float]) -> list[str]:
    """The message bodies for `device` that can belong to this join.

    A line stamped before `since` is dropped: journalctl's `--since` is
    truncated to the second, and missing a line is better than counting a
    stale one. Everything up to and including the first
    `CTRL-EVENT-DISCONNECTED ... locally_generated=1` is dropped too: that
    line is the previous connection being taken down by `connection up`, as
    each bench capture begins with it, and the lines before it, an anchor on
    the same SSID among them, are the old profile's.

    Still possible: when no local disconnect appears (the device was idle,
    or the previous connection had already dropped and NetworkManager was
    retrying it), a line of the previous profile stamped at or after
    `since` is counted; lines without a timestamp (`-o cat`,
    `-o short-precise`, which the join does not read) are not filtered by
    time at all; and a locally generated disconnect inside this join's own
    attempts discards this join's earlier lines, so the outcome degrades to
    timeout or failed.
    """
    prefix = f"{device}: "
    bodies = []
    for raw in lines:
        stamp, message = _message(raw)
        if not message.startswith(prefix):
            continue
        if since is not None and stamp is not None and stamp < since:
            continue
        bodies.append(message[len(prefix):])
    for i, body in enumerate(bodies):
        if body.startswith("CTRL-EVENT-DISCONNECTED") and "locally_generated=1" in body:
            return bodies[i + 1:]
    return bodies


# An attempt on a network: "SME: Trying to authenticate with <bssid> (SSID='<ssid>' freq=<n> MHz)"
# and "Trying to associate with <bssid> (SSID='<ssid>' freq=<n> MHz)", as in the bench
# captures; "Trying to associate with SSID '<ssid>'" is wpa_supplicant's form without a BSSID.
_ATTEMPT = re.compile(r"^(?:SME: )?Trying to (?:authenticate|associate) with "
                      r"(?:\S+ \(SSID='(?P<a>.*)' freq=\d+ MHz\)|SSID '(?P<b>.*)')$")
_TEMP_DISABLED_SSID = re.compile(r'^CTRL-EVENT-SSID-TEMP-DISABLED id=\d+ ssid="(.*)" auth_failures=')
# Lines that can come from causes other than a wrong secret: a reason-15
# disconnect at weak signal prints the 4-Way Handshake line, for instance.
# Each decides on its second occurrence for this join.
# (marker, also required, outcome). SAE prints neither of the PSK lines on a
# wrong password: the unit printed `CTRL-EVENT-AUTH-REJECT <bssid> auth_type=3
# auth_transaction=2 status_code=1` for each attempt, then reason=CONN_FAILED.
_SOFT = (
    ("CTRL-EVENT-EAP-FAILURE", None, "wrong_credentials"),
    ("reason=AUTH_FAILED", None, "wrong_credentials"),
    ("4-Way Handshake failed", None, "wrong_password"),
    ("reason=WRONG_KEY", None, "wrong_password"),
    ("CTRL-EVENT-AUTH-REJECT", "auth_type=3", "wrong_password"),
)


def _wpa_ssid_text(ssid: str) -> str:
    """An SSID as wpa_supplicant prints it (its printf_encode).

    The double quote and the backslash are escaped with a backslash; ESC,
    newline, carriage return and tab are written as backslash-e, -n, -r and
    -t; other bytes outside printable ASCII as backslash-x and two hex digits.
    """
    named = {0x22: '\\"', 0x5C: "\\\\", 0x1B: "\\e", 0x0A: "\\n", 0x0D: "\\r", 0x09: "\\t"}
    out = []
    for byte in ssid.encode("utf-8"):
        if byte in named:
            out.append(named[byte])
        elif 32 <= byte <= 126:
            out.append(chr(byte))
        else:
            out.append(f"\\x{byte:02x}")
    return "".join(out)


def classify(lines: list[str], device: str, ssid: str, *, final: bool = False,
             since: Optional[float] = None) -> Optional[dict]:
    """The failure of this join in wpa_supplicant lines, or None.

    Accepts `-o cat`, `-o short-precise` and `-o short-unix` lines; only
    messages that begin with "<device>: " and that _this_join keeps count,
    and only after the first attempt on `ssid`
    ("Trying to authenticate/associate with ... SSID='<ssid>'"): the journal
    window opens just before `connection up`, truncated to the second, so it
    can hold lines of the connection that was active before (see
    _this_join). Before that anchor nothing is
    decisive. A CTRL-EVENT-SSID-TEMP-DISABLED line naming another network is
    ignored.

    Definitive, at the first occurrence: a certificate error (domain suffix
    mismatch -> server_name_mismatch, any other -> server_not_trusted, with
    the server's depth-0 certificate of that attempt) and MSCHAPv2's error
    691 (wrong_credentials). A certificate error outranks the EAP-FAILURE
    that follows it.

    Weaker (_SOFT): CTRL-EVENT-EAP-FAILURE and reason=AUTH_FAILED
    (wrong_credentials), "4-Way Handshake failed" and reason=WRONG_KEY
    (wrong_password), and for SAE CTRL-EVENT-AUTH-REJECT with auth_type=3
    (wrong_password) decide when the same line occurs a second time. One
    failed PSK attempt prints both of its lines once, so it is not decisive.
    A weaker line that reaches its second occurrence is not returned at
    once: the rest of the lines are read first, and CTRL-EVENT-CONNECTED
    clears the counts and any such outcome: a failure followed by a
    connection is a join, not a failure. The outcome still standing at the
    end is returned, with or without `final`; of two, the one that reached
    its second occurrence first. A definitive line still returns at once.

    With `final` (nmcli has ended without joining), a weaker line seen once
    since the last CTRL-EVENT-CONNECTED is the outcome as well.
    """
    target = _wpa_ssid_text(ssid)
    anchored = False
    server: Optional[dict] = None
    counts: dict[str, int] = {}
    first_soft: Optional[str] = None
    decided: Optional[str] = None
    for body in _this_join(lines, device, since):
        attempt = _ATTEMPT.match(body)
        if not anchored:
            if attempt and (attempt.group("a") if attempt.group("a") is not None else attempt.group("b")) == target:
                anchored = True
            continue
        if body.startswith("CTRL-EVENT-EAP-STARTED"):
            server = None
            continue
        if body.startswith("CTRL-EVENT-CONNECTED"):
            counts.clear()
            first_soft = None
            decided = None
            continue
        disabled = _TEMP_DISABLED_SSID.match(body)
        if disabled and disabled.group(1) != target:
            continue
        cert = _PEER_CERT.match(body)
        if cert:
            server = {"subject": cert.group(1), "names": [], "hash": cert.group(2)}
            continue
        alt = _PEER_ALT.match(body)
        if alt:
            if server is not None:
                name = alt.group(1)
                server["names"].append(name[4:] if name.startswith("DNS:") else name)
            continue
        if body.startswith("CTRL-EVENT-EAP-TLS-CERT-ERROR"):
            if "err='Domain suffix mismatch'" in body:
                return {"code": "server_name_mismatch", "detail": None, "server": server}
            err = _CERT_ERR.search(body)
            return {"code": "server_not_trusted", "detail": err.group(1) if err else None, "server": server}
        if body.startswith("EAP-MSCHAPV2: failure message") and "error 691" in body:
            return _problem("wrong_credentials")
        for marker, required, code in _SOFT:
            if marker in body and (required is None or required in body):
                counts[marker] = counts.get(marker, 0) + 1
                if counts[marker] >= 2 and decided is None:
                    decided = code
                if first_soft is None:
                    first_soft = code
                break
    if decided is not None:
        return _problem(decided)
    if final and first_soft is not None:
        return _problem(first_soft)
    return None


def _journal(since: float) -> Optional[list[str]]:
    """wpa_supplicant's journal lines since `since`; None when it cannot be read. Never raises.

    journalctl rejects a fractional `@<epoch>`, so the epoch is truncated;
    `-o short-unix` keeps each line's own epoch so classify can drop the
    lines of that second that came before `since`."""
    try:
        result = _command(["journalctl", "-t", "wpa_supplicant", "--since", f"@{int(since)}", "-o", "short-unix",
                           "--no-pager"], timeout=_JOURNAL_TIMEOUT)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _lines(result.stdout)


# ---------------------------------------------------------------------------
# Join: the sequence
# ---------------------------------------------------------------------------

@dataclass
class JoinOutcome:
    joined: bool
    problem: Optional[dict] = None
    restored: Optional[bool] = None


def _problem(code: str) -> dict:
    return {"code": code, "detail": None, "server": None}


def _not_permitted(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return "not authorized" in lowered or "insufficient privileges" in lowered


def _active_uuid(device: str) -> Optional[str]:
    fields = parse_fields(_read(["nmcli", "-t", "-f", "GENERAL.CON-UUID", "device", "show", device]))
    values = _values(fields, "GENERAL.CON-UUID")
    return values[0] if values else None


def _delete(uuid: str) -> None:
    try:
        result = _command(["nmcli", "connection", "delete", "uuid", uuid], timeout=_WRITE_TIMEOUT)
        if result.returncode != 0:
            logger.warning("Could not delete Wi-Fi profile %s", uuid)
    except NetworkUnavailable:
        logger.warning("Could not delete Wi-Fi profile %s", uuid)


def _restore(previous: str, device: str) -> bool:
    """Bring back the connection that was active before the join; true when
    it is active or being activated again.

    NetworkManager often reconnects by itself once the failed profile is
    deleted, so the device's active connection is read first and
    `connection up` runs only when it is not already `previous`. The device
    reports that UUID while the profile is still activating too (seen on the
    unit as `connecting (configuring)`, active 11.7 s later), so true does
    not mean the link is up yet."""
    try:
        active = _active_uuid(device)
    except NetworkUnavailable:
        active = None
    if active is not None and active.lower() == previous.lower():
        return True
    try:
        result = _command(["nmcli", "--wait", str(_RESTORE_WAIT), "connection", "up", "uuid", previous,
                           "ifname", device], timeout=_RESTORE_TIMEOUT)
    except NetworkUnavailable:
        return False
    return result.returncode == 0


def _remove_others(ssid: str, keep: str) -> None:
    try:
        others = [s["uuid"] for s in saved_networks() if s["ssid"] == ssid and s["uuid"] != keep]
    except NetworkUnavailable:
        logger.warning("Joined %r but could not list its other saved profiles", ssid)
        return
    for uuid in others:
        _delete(uuid)


def join(*, device: str, ssid: str, security: str, password: Optional[str] = None,
         identity: Optional[str] = None, method: Optional[str] = None, inner_method: Optional[str] = None,
         anonymous_identity: Optional[str] = None, server_name: Optional[str] = None) -> JoinOutcome:
    """Join `ssid` on `device`, synchronously. Inputs are those join_problems accepted.

    1. Record the connection active on the device.
    2. Add the profile, without a secret, under a UUID generated here.
    3. Activate it by that UUID, the secret on stdin, reading the journal
       about once a second and stopping on a decisive line.
    4. Joined: delete the other saved profiles for this SSID.
    5. Otherwise: delete the new profile (in a finally, so an exception does
       too), which stops NetworkManager's retries, then restore the recorded
       connection (_restore).
    """
    try:
        previous = _active_uuid(device)
    except NetworkUnavailable:
        return JoinOutcome(joined=False, problem=_problem("failed"))

    new_uuid = str(_uuid4())
    argv = _profile_args(device=device, ssid=ssid, security=security, profile_uuid=new_uuid, identity=identity,
                         method=method, inner_method=inner_method, anonymous_identity=anonymous_identity,
                         server_name=server_name)
    secrets = _secrets_text(security=security, password=password, identity=identity)
    up_argv = ["nmcli", "--wait", str(_UP_WAIT), "connection", "up", "uuid", new_uuid, "ifname", device]
    if secrets is not None:
        up_argv += ["passwd-file", "/dev/stdin"]

    outcome = JoinOutcome(joined=False)
    # Until nmcli answers that the add failed, the profile may exist.
    created = True
    try:
        # NOTE: a process restart between this add and the delete below leaves
        # the profile behind; it shows under the saved networks and can be
        # forgotten there.
        try:
            added = _command(argv, timeout=_WRITE_TIMEOUT)
        except NetworkUnavailable:
            outcome.problem = _problem("failed")
            return outcome
        if added.returncode != 0:
            created = False
            outcome.problem = _problem("not_permitted" if _not_permitted(added.stderr) else "failed")
            return outcome
        del argv, added

        lines: list[str] = []
        verdict: dict = {}

        def tick() -> bool:
            read = _journal(since)
            if read is not None:
                lines[:] = read
            found_problem = classify(lines, device, ssid, since=since)
            if found_problem is not None:
                verdict["problem"] = found_problem
                return True
            return False

        # The journal window opens now, after the add and just before the
        # activation that takes the previous connection down.
        since = _wall_clock()
        try:
            result = _command(up_argv, timeout=_UP_TIMEOUT, on_tick=tick, input_text=secrets)
        except NetworkUnavailable:
            result = None

        if result is not None and not result.stopped and result.returncode == 0:
            outcome.joined = True
        else:
            problem = verdict.get("problem")
            if problem is None:
                # nmcli may end before a tick has seen the line that explains
                # it; once it has ended, one weaker failure line is enough.
                read = _journal(since)
                if read is not None:
                    lines[:] = read
                problem = classify(lines, device, ssid, final=True, since=since)
            if problem is None:
                if result is None or result.returncode == _NMCLI_TIMEOUT_EXIT:
                    problem = _problem("timeout")
                elif _not_permitted(result.stderr):
                    problem = _problem("not_permitted")
                else:
                    problem = _problem("failed")
            outcome.problem = problem
    finally:
        secrets = None
        if not outcome.joined and created:
            _delete(new_uuid)
            if previous and previous.lower() != new_uuid:
                outcome.restored = _restore(previous, device)
    if not outcome.joined:
        return outcome

    try:
        _remove_others(ssid, new_uuid)
    except Exception as exc:
        logger.warning("Joined %r but could not remove its other saved profiles (%s)", ssid, type(exc).__name__)
    return outcome


# ---------------------------------------------------------------------------
# Join: the state of the last attempt
# ---------------------------------------------------------------------------

_join_lock = threading.Lock()
_join_state: dict = {}


def _idle() -> dict:
    return {"state": "idle", "ssid": None, "startedAt": None, "finishedAt": None, "problem": None,
            "restored": None}


def _reset_join_state() -> None:
    with _join_lock:
        _join_state.clear()
        _join_state.update(_idle())


_reset_join_state()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def join_state() -> dict:
    """GET /network/wifi/join: the last attempt. Holds no password, identity or anonymous identity."""
    with _join_lock:
        state = dict(_join_state)
    if state["problem"] is not None:
        state["problem"] = dict(state["problem"])
    return state


def claim_join(*, ssid: str, security: str, password: Optional[str] = None, identity: Optional[str] = None,
               method: Optional[str] = None, inner_method: Optional[str] = None,
               anonymous_identity: Optional[str] = None, server_name: Optional[str] = None) -> str:
    """Check a join request and mark a join as started; returns the Wi-Fi device.

    Raises NetworkRefused with the codes of join_problems, then
    no_wifi_device, then not_permitted (polkit does not allow
    network-control or settings.modify.system), then join_in_progress;
    NetworkUnavailable when the device list or the permissions cannot be
    read. Only those two reads have run when it raises."""
    codes = join_problems(ssid=ssid, security=security, password=password, identity=identity, method=method,
                          inner_method=inner_method, server_name=server_name,
                          anonymous_identity=anonymous_identity)
    if codes:
        raise NetworkRefused(codes)
    device = wifi_device()
    if device is None:
        raise NetworkRefused(["no_wifi_device"])
    allowed = permissions()
    if not (allowed["control"] and allowed["modify"]):
        raise NetworkRefused(["not_permitted"])
    with _join_lock:
        if _join_state["state"] == "joining":
            raise NetworkRefused(["join_in_progress"])
        _join_state.clear()
        _join_state.update(_idle())
        _join_state.update({"state": "joining", "ssid": ssid, "startedAt": _now()})
    return device


def finish_join(outcome: JoinOutcome) -> None:
    with _join_lock:
        _join_state.update({
            "state": "joined" if outcome.joined else "failed",
            "finishedAt": _now(),
            "problem": outcome.problem,
            "restored": outcome.restored,
        })


def run_join(**kwargs) -> None:
    """The join thread's body: join, then record the outcome. Never raises."""
    ssid = kwargs.get("ssid")
    try:
        outcome = join(**kwargs)
    except Exception as exc:
        # The type only: an exception's text is not trusted to be free of secrets.
        logger.warning("Joining Wi-Fi network %r raised %s", ssid, type(exc).__name__)
        outcome = JoinOutcome(joined=False, problem=_problem("failed"))
    finish_join(outcome)
    if outcome.joined:
        logger.info("Joined Wi-Fi network %r", ssid)
    else:
        logger.warning("Could not join Wi-Fi network %r: %s (restored: %s)", ssid, outcome.problem["code"],
                       outcome.restored)
