"""The network core (NEH-253): status, Wi-Fi scan, join and forget over nmcli.

No test runs a real nmcli or journalctl and none sleeps: every command goes
through app.core.network._spawn, which FakeNM replaces, and the clock the
runner reads is a FakeClock that the fake child advances while it is waited
on. The nmcli-*.txt fixtures are synthetic; the journal-*.txt fixtures are
captures from the bench unit.

    python -m pytest tests/unit/test_network_core.py

Version: 0.3.3
"""

import logging
import os
import subprocess
import uuid
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "network"

PASSWORD = "s3cret-Pa55word"
IDENTITY = "tester@example.edu"
ANON = "anonymous@example.edu"
NEW_UUID = "11111111-2222-4333-8444-555555555555"
OLD_UUID = "2b6c0c2e-8f1a-4f53-9d1e-6a0f9b1c2d3e"
EDUROAM_UUID = "9a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# The device and the SSID each bench capture was taken with.
CAPTURED = {
    "journal-wrong-password.txt": ("wlan2", "captua-eap-test"),
    "journal-server-name-mismatch.txt": ("wlan2", "captua-eap-test"),
    "journal-untrusted-ca.txt": ("wlan2", "captua-eap-test"),
    "journal-ca-not-in-system-store.txt": ("wlan2", "captua-eap-test"),
    "journal-psk-wrong-key.txt": ("wlan6", "captua-B"),
    "journal-eap-wrong-password-stdin.txt": ("wlan2", "captua-E"),
}


def journal_for(name, device="wlan0", ssid="Captua Lab"):
    """A bench capture as if taken on `device` while joining `ssid`."""
    old_device, old_ssid = CAPTURED[name]
    return (fixture(name).replace(f"{old_device}: ", f"{device}: ")
            .replace(f"SSID='{old_ssid}'", f"SSID='{ssid}'").replace(f'ssid="{old_ssid}"', f'ssid="{ssid}"'))


# ---------------------------------------------------------------------------
# The fake command layer
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t


class Spec:
    """What one command does: exit code, output, and how many one-second
    waits pass before it exits (None: it never exits on its own)."""

    def __init__(self, rc=0, stdout="", stderr="", waits=0, raises=None, undecodable=False):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.waits, self.raises = waits, raises
        self.undecodable = undecodable


class FakeChild:
    def __init__(self, nm, argv, spec):
        self.nm, self.argv, self.spec = nm, list(argv), spec
        self.waits = spec.waits
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.input = None

    def _out(self, value):
        return value(self.nm) if callable(value) else value

    def communicate(self, input=None, timeout=None):
        if input is not None:
            if self.input is not None:
                raise AssertionError("input sent twice")
            self.input = input
        if self.terminated or self.killed:
            self.returncode = -15 if self.terminated else -9
            return "", ""
        if self.waits is None or self.waits > 0:
            if timeout is None:
                raise AssertionError(f"{self.argv[:3]} waited on with no timeout")
            if self.waits is not None:
                self.waits -= 1
            self.nm.clock.t += timeout
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = self._out(self.spec.rc)
        if self.spec.undecodable:
            # What text-mode communicate() raises on output that is not UTF-8.
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return self._out(self.spec.stdout), self._out(self.spec.stderr)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class FakeNM:
    """Answers each argv by the longest registered prefix of its joined text."""

    def __init__(self, clock):
        self.clock = clock
        self.specs = {}
        self.calls = []
        self.kwargs = []
        self.children = []
        # The active connection the device reports once the new profile is deleted.
        self.active_after = ""

    def on(self, prefix, **spec):
        self.specs[prefix] = Spec(**spec)
        return self

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        text = " ".join(argv)
        matches = [p for p in self.specs if text.startswith(p)]
        if not matches:
            raise AssertionError(f"unexpected command: {argv[:6]}")
        spec = self.specs[max(matches, key=len)]
        if spec.raises is not None:
            raise spec.raises
        child = FakeChild(self, argv, spec)
        self.children.append(child)
        return child

    def ran(self, prefix):
        return [c for c in self.calls if " ".join(c).startswith(prefix)]

    def child(self, prefix):
        return [c for c in self.children if " ".join(c.argv).startswith(prefix)]

    def inputs(self):
        return [c.input for c in self.children if c.input is not None]


DEVICE_STATUS = "nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status"
PERMISSIONS = "nmcli -t general permissions"
SHOW_WLAN0 = "nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show wlan0"
WIFI_LIST_CACHED = "nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list ifname wlan0 --rescan no"
WIFI_LIST_SCAN = "nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list ifname wlan0 --rescan auto"
WIFI_LIST_RESCAN = "nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list ifname wlan0 --rescan yes"
CONNECTIONS = "nmcli -t -f NAME,UUID,TYPE,ACTIVE connection show"
DETAILS = "nmcli -t -f 802-11-wireless.ssid,802-11-wireless-security.key-mgmt connection show uuid"
CON_UUID = "nmcli -t -f GENERAL.CON-UUID device show wlan0"
ADD = "nmcli connection add"
UP_NEW = f"nmcli --wait 45 connection up uuid {NEW_UUID} ifname wlan0"
UP_NEW_STDIN = f"{UP_NEW} passwd-file /dev/stdin"
UP_OLD = f"nmcli --wait 30 connection up uuid {OLD_UUID} ifname wlan0"
DELETE = "nmcli connection delete uuid"
JOURNAL = "journalctl -t wpa_supplicant"


def details(ssid, key_mgmt=""):
    return f"802-11-wireless.ssid:{ssid}\n802-11-wireless-security.key-mgmt:{key_mgmt}\n"


@pytest.fixture
def nm(monkeypatch):
    from app.core import config, network
    clock = FakeClock()
    fake = FakeNM(clock)
    monkeypatch.setattr(network, "_spawn", fake)
    monkeypatch.setattr(network, "_monotonic", clock)
    monkeypatch.setattr(network, "_wall_clock", lambda: 1794419003.4)
    monkeypatch.setattr(network, "_uuid4", lambda: uuid.UUID(NEW_UUID))
    monkeypatch.setattr(config.settings, "WIFI_DEVICE", None, raising=False)
    network._reset_join_state()
    fake.on(DEVICE_STATUS, stdout=fixture("nmcli-device-status.txt"))
    fake.on(PERMISSIONS, stdout=fixture("nmcli-general-permissions-pi-rule-installed.txt"))
    fake.on(SHOW_WLAN0, stdout=fixture("nmcli-device-show-wlan0.txt"))
    fake.on(WIFI_LIST_CACHED, stdout=fixture("nmcli-wifi-list.txt"))
    fake.on(WIFI_LIST_SCAN, stdout=fixture("nmcli-wifi-list.txt"))
    fake.on(CONNECTIONS, stdout=fixture("nmcli-connection-show.txt"))
    fake.on(f"{DETAILS} {OLD_UUID}", stdout=details("Captua Lab", "wpa-psk"))
    fake.on(f"{DETAILS} {EDUROAM_UUID}", stdout=details("eduroam", "wpa-eap"))
    yield fake
    network._reset_join_state()


def _join_nm(nm, *, up_rc=0, up_waits=2, up_stderr="", journal=""):
    """The commands of one join on wlan0, whose active connection is OLD_UUID."""
    # Before the join the device has OLD_UUID; once the new profile is
    # deleted it has nm.active_after (nothing, unless a test says otherwise).
    nm.on(CON_UUID, stdout=lambda n: f"GENERAL.CON-UUID:{n.active_after if n.ran(DELETE) else OLD_UUID}\n")
    # nmcli's answer names another UUID: the join must not read it.
    nm.on(ADD, stdout="Connection 'Captua Lab' (00000000-0000-4000-8000-000000000000) successfully added.\n")
    nm.on(UP_NEW, rc=up_rc, waits=up_waits, stderr=up_stderr)
    nm.on(UP_OLD, rc=0)
    nm.on(DELETE, rc=0)
    nm.on(JOURNAL, stdout=journal)
    return nm


def _journal_after(nm, seconds, text):
    """journalctl output that holds `text` once `seconds` have passed on the fake clock."""
    start = nm.clock.t
    return lambda _nm: text if nm.clock.t - start >= seconds else ""


# ---------------------------------------------------------------------------
# Terse parsing
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_split_terse_unescapes_colons_and_backslashes():
    from app.core.network import split_terse
    assert split_terse(r"wlan0:wifi:connected:Back\\slash\:Net") == ["wlan0", "wifi", "connected", "Back\\slash:Net"]
    assert split_terse(r"GENERAL.HWADDR:DC\:A6\:32\:01\:02\:03", maxsplit=1) == ["GENERAL.HWADDR", "DC:A6:32:01:02:03"]
    assert split_terse(" ::30:WPA2") == [" ", "", "30", "WPA2"]


@pytest.mark.unit
def test_parse_fields_collects_multi_value_fields():
    from app.core.network import parse_fields
    fields = parse_fields(fixture("nmcli-device-show-wlan0.txt"))
    assert fields["IP4.DNS"] == ["192.168.1.1", "8.8.8.8"]
    assert fields["IP4.ADDRESS"] == ["192.168.1.23/24"]
    assert fields["IP6.ADDRESS"] == ["fe80::dea6:32ff:fe01:203/64"]
    assert fields["GENERAL.HWADDR"] == ["DC:A6:32:01:02:03"]
    assert fields["IP6.GATEWAY"] == [""]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_status_lists_only_ethernet_and_wifi(nm):
    from app.core import network
    nm.on("nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show eth0",
          stdout="GENERAL.HWADDR:DC:A6:32:0A:0B:0C\n")
    body = network.status()
    assert [(i["device"], i["type"]) for i in body["interfaces"]] == [("wlan0", "wifi"), ("eth0", "ethernet")]
    shown = [c[-1] for c in nm.ran("nmcli -t -f GENERAL.HWADDR")]
    assert shown == ["wlan0", "eth0"]


@pytest.mark.unit
def test_status_reports_the_connected_wifi_device(nm, monkeypatch):
    from app.core import network
    monkeypatch.setattr(network.socket, "gethostname", lambda: "captua-07")
    nm.on("nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show eth0", stdout="")
    body = network.status()
    assert body["hostname"] == "captua-07"
    assert "connectivity" not in body
    assert body["permissions"] == {"scan": True, "control": True, "modify": True}
    assert body["wifiDevice"] == "wlan0"
    wlan0 = body["interfaces"][0]
    assert wlan0 == {
        "device": "wlan0", "type": "wifi", "state": "connected", "connection": "Captua Lab",
        "mac": "DC:A6:32:01:02:03", "ipv4": ["192.168.1.23/24"], "ipv6": ["fe80::dea6:32ff:fe01:203/64"],
        "gateway": "192.168.1.1", "dns": ["192.168.1.1", "8.8.8.8"], "ssid": "Captua Lab", "signal": 72,
    }
    eth0 = body["interfaces"][1]
    assert eth0["state"] == "unavailable" and eth0["connection"] is None and eth0["ssid"] is None
    assert body["problems"] == []
    # The cached list, never a rescan; NetworkManager's connectivity is not read.
    assert nm.ran(WIFI_LIST_CACHED) and not nm.ran(WIFI_LIST_SCAN)
    assert not [c for c in nm.calls if "check" in c or "connectivity" in c or "CONNECTIVITY" in " ".join(c)]
    assert not nm.ran("nmcli -t -f CONNECTIVITY") and not nm.ran("nmcli general status")


@pytest.mark.unit
def test_status_with_nothing_connected_reports_no_connection(nm):
    from app.core import network
    nm.on(DEVICE_STATUS, stdout="wlan0:wifi:disconnected:--\neth0:ethernet:unavailable:\n")
    nm.on("nmcli -t -f GENERAL.HWADDR", stdout="")
    body = network.status()
    assert body["problems"] == [{"code": "no_connection", "device": None}]
    assert all(i["connection"] is None for i in body["interfaces"])


def _connected_eth(nm, show):
    nm.on(DEVICE_STATUS, stdout="eth0:ethernet:connected:Wired connection 1\n")
    nm.on("nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show eth0", stdout=show)


@pytest.mark.unit
@pytest.mark.parametrize("show, codes", [
    ("IP4.GATEWAY:10.0.0.1\nIP4.DNS[1]:10.0.0.1\n", ["no_address"]),
    ("IP4.ADDRESS[1]:10.0.0.5/24\nIP4.GATEWAY:\nIP4.DNS[1]:10.0.0.1\n", ["no_gateway"]),
    ("IP4.ADDRESS[1]:10.0.0.5/24\nIP4.GATEWAY:--\n", ["no_gateway", "no_dns"]),
    ("IP4.ADDRESS[1]:10.0.0.5/24\nIP4.GATEWAY:10.0.0.1\n", ["no_dns"]),
    ("IP4.ADDRESS[1]:169.254.10.4/16\n", ["link_local_only"]),
    ("IP4.ADDRESS[1]:169.254.10.4/16\nIP4.ADDRESS[2]:10.0.0.5/24\n", ["no_gateway", "no_dns"]),
])
def test_status_problem_codes(nm, show, codes):
    from app.core import network
    _connected_eth(nm, show)
    body = network.status()
    assert body["problems"] == [{"code": c, "device": "eth0"} for c in codes]
    assert body["wifiDevice"] is None


@pytest.mark.unit
@pytest.mark.parametrize("spec", [dict(rc=8, stderr="Error: NetworkManager is not running."),
                                  dict(waits=None), dict(raises=FileNotFoundError("nmcli"))])
def test_status_read_failure_raises_unavailable(nm, spec):
    from app.core import network
    nm.on(DEVICE_STATUS, **spec)
    with pytest.raises(network.NetworkUnavailable):
        network.status()


@pytest.mark.unit
def test_every_command_is_an_argv_list_with_stdin_closed(nm, monkeypatch):
    from app.core import network
    monkeypatch.setenv("CAPTUA_PROBE", "kept")
    monkeypatch.setenv("LC_ALL", "es_CO.UTF-8")
    nm.on("nmcli -t -f GENERAL.HWADDR", stdout="")
    network.status()
    assert nm.kwargs
    for kwargs in nm.kwargs:
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.PIPE
        assert kwargs["text"] is True
        assert not kwargs.get("shell")
        # English output for the parsers; the rest of the environment is kept.
        assert kwargs["env"]["LC_ALL"] == "C.UTF-8"
        assert kwargs["env"]["CAPTUA_PROBE"] == "kept"
        assert kwargs["env"]["PATH"] == os.environ["PATH"]


@pytest.mark.unit
def test_available_follows_which(monkeypatch):
    from app.core import network
    monkeypatch.setattr(network.shutil, "which", lambda name: None)
    assert network.available() is False
    monkeypatch.setattr(network.shutil, "which", lambda name: "/usr/bin/nmcli" if name == "nmcli" else None)
    assert network.available() is True


# ---------------------------------------------------------------------------
# The Wi-Fi device
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_wifi_device_is_the_first_wifi_row(nm):
    from app.core import network
    nm.on(DEVICE_STATUS, stdout="eth0:ethernet:connected:x\nwlan2:wifi:disconnected:\nwlan0:wifi:connected:y\n")
    assert network.wifi_device() == "wlan2"


@pytest.mark.unit
def test_configured_wifi_device_wins_over_discovery(nm, monkeypatch):
    from app.core import config, network
    monkeypatch.setattr(config.settings, "WIFI_DEVICE", "wlan9", raising=False)
    nm.on(DEVICE_STATUS, stdout=fixture("nmcli-device-status.txt") + "wlan9:wifi:disconnected:\n")
    nm.on("nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list ifname wlan9", stdout="")
    nm.on("nmcli -t -f GENERAL.CON-UUID device show wlan9", stdout="GENERAL.CON-UUID:\n")
    assert network.wifi_device() == "wlan9"
    network.scan()
    scans = nm.ran("nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list")
    assert scans and all(c[c.index("ifname") + 1] == "wlan9" for c in scans)
    assert network.claim_join(ssid="Captua Lab", security="open") == "wlan9"


@pytest.mark.unit
def test_no_wifi_device_refuses_scan_and_join(nm):
    from app.core import network
    nm.on(DEVICE_STATUS, stdout="eth0:ethernet:connected:x\n")
    with pytest.raises(network.NetworkRefused) as exc:
        network.scan()
    assert exc.value.codes == ["no_wifi_device"]
    with pytest.raises(network.NetworkRefused) as exc:
        network.claim_join(ssid="Captua Lab", security="open")
    assert exc.value.codes == ["no_wifi_device"]
    assert network.join_state()["state"] == "idle"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_scan_kinds_dedupe_order_and_saved(nm):
    from app.core import network
    body = network.scan()
    assert body["networks"] == [
        {"ssid": "Captua Lab", "signal": 72, "security": "password", "inUse": True, "saved": True},
        {"ssid": "eduroam", "signal": 80, "security": "enterprise", "inUse": False, "saved": True},
        {"ssid": "NewHome", "signal": 60, "security": "password", "inUse": False, "saved": False},
        {"ssid": "Library Guest", "signal": 55, "security": "open", "inUse": False, "saved": False},
        {"ssid": "Studio:5G", "signal": 48, "security": "password", "inUse": False, "saved": False},
        {"ssid": "Old Cafe", "signal": 20, "security": "open", "inUse": False, "saved": False},
    ]
    assert body["saved"] == [
        {"uuid": OLD_UUID, "name": "Captua Lab", "ssid": "Captua Lab", "security": "password", "active": True},
        {"uuid": EDUROAM_UUID, "name": "eduroam", "ssid": "eduroam", "security": "enterprise", "active": False},
    ]
    assert nm.ran(WIFI_LIST_SCAN)
    assert set(body) == {"networks", "saved", "rescanned"} and body["rescanned"] is False


@pytest.mark.unit
def test_saved_open_profile_and_details_fallback(nm):
    from app.core import network
    nm.on(CONNECTIONS, stdout=f"Library Guest:{NEW_UUID}:802-11-wireless:no\n")
    nm.on(f"{DETAILS} {NEW_UUID}", rc=10, stderr="Error: invalid field '802-11-wireless-security'")
    nm.on(f"nmcli -t -f 802-11-wireless.ssid connection show uuid {NEW_UUID}",
          stdout="802-11-wireless.ssid:Library Guest\n")
    assert network.saved_networks() == [
        {"uuid": NEW_UUID, "name": "Library Guest", "ssid": "Library Guest", "security": "open", "active": False},
    ]


@pytest.mark.unit
def test_no_command_reads_a_saved_secret(nm):
    from app.core import network
    network.scan()
    network.saved_networks()
    for argv in nm.calls:
        assert "-s" not in argv and "--show-secrets" not in argv
        for i, arg in enumerate(argv):
            if arg in ("-f", "-g", "--fields", "--get-values"):
                assert "psk" not in argv[i + 1] and "password" not in argv[i + 1]


# ---------------------------------------------------------------------------
# Journal classification against the bench captures
# ---------------------------------------------------------------------------

SERVER = {
    "subject": "/CN=radius.test.example.edu",
    "names": ["radius.test.example.edu"],
    "hash": "3b4f37b7f2a7c3a819433c66a84b354b0d3b6a592a3115e2427e75a5d0d4890e",
}


@pytest.mark.unit
def test_classify_wrong_password():
    from app.core.network import classify
    lines = fixture("journal-wrong-password.txt").splitlines()
    assert classify(lines, "wlan2", "captua-eap-test") == {"code": "wrong_credentials", "detail": None, "server": None}


@pytest.mark.unit
def test_classify_server_name_mismatch():
    from app.core.network import classify
    lines = fixture("journal-server-name-mismatch.txt").splitlines()
    assert classify(lines, "wlan2", "captua-eap-test") == {"code": "server_name_mismatch", "detail": None,
                                                          "server": SERVER}


@pytest.mark.unit
@pytest.mark.parametrize("name", ["journal-untrusted-ca.txt", "journal-ca-not-in-system-store.txt"])
def test_classify_untrusted_certificate(name):
    from app.core.network import classify
    lines = fixture(name).splitlines()
    assert classify(lines, "wlan2", "captua-eap-test") == {
        "code": "server_not_trusted", "detail": "self-signed certificate in certificate chain", "server": SERVER,
    }


@pytest.mark.unit
@pytest.mark.parametrize("name", ["journal-wrong-password.txt", "journal-server-name-mismatch.txt",
                                  "journal-untrusted-ca.txt", "journal-ca-not-in-system-store.txt"])
def test_classify_ignores_lines_for_another_device(name):
    from app.core.network import classify
    assert classify(fixture(name).splitlines(), "wlan0", "captua-eap-test") is None
    assert classify(fixture(name).splitlines(), "wlan", "captua-eap-test") is None
    assert classify(fixture(name).splitlines(), "wlan0", "captua-eap-test", final=True) is None


@pytest.mark.unit
def test_classify_reads_cat_output():
    """`-o cat` gives the message alone, as journalctl prints it to the join."""
    from app.core.network import classify
    lines = [l.split("wpa_supplicant[855]: ", 1)[1] for l in fixture("journal-server-name-mismatch.txt").splitlines()
             if "wpa_supplicant[855]: " in l]
    assert classify(lines, "wlan2", "captua-eap-test")["code"] == "server_name_mismatch"


@pytest.mark.unit
def test_classify_wrong_key_once_decides_only_at_the_end():
    from app.core.network import classify
    lines = ["wlan0: SME: Trying to authenticate with 02:00:00:00:01:00 (SSID='Home' freq=2412 MHz)",
             "wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"Home\" auth_failures=1 duration=10 reason=WRONG_KEY"]
    assert classify(lines, "wlan0", "Home") is None
    assert classify(lines, "wlan0", "Home", final=True) == {"code": "wrong_password", "detail": None, "server": None}
    assert classify(lines + lines[1:], "wlan0", "Home") == {"code": "wrong_password", "detail": None, "server": None}


@pytest.mark.unit
def test_classify_nothing_decisive():
    from app.core.network import classify
    lines = fixture("journal-wrong-password.txt").splitlines()[:35]
    assert classify(lines, "wlan2", "captua-eap-test") is None
    assert classify(lines, "wlan2", "captua-eap-test", final=True) is None
    assert classify([], "wlan2", "captua-eap-test") is None


@pytest.mark.unit
def test_a_certificate_error_outranks_the_failure_that_follows():
    from app.core.network import classify
    lines = [
        "wlan0: Trying to associate with 02:00:00:00:00:00 (SSID='eduroam' freq=2437 MHz)",
        "wlan0: CTRL-EVENT-EAP-STARTED EAP authentication started",
        "wlan0: CTRL-EVENT-EAP-TLS-CERT-ERROR reason=1 depth=1 subject='/CN=X' err='unable to get local issuer certificate'",
        "wlan0: CTRL-EVENT-EAP-FAILURE EAP authentication failed",
    ]
    assert classify(lines, "wlan0", "eduroam") == {
        "code": "server_not_trusted", "detail": "unable to get local issuer certificate", "server": None,
    }


# ---------------------------------------------------------------------------
# Server validation and refusals
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("method, inner, anon", [
    ("peap", None, None), ("peap", "mschapv2", ANON), ("ttls", None, None), ("ttls", "pap", ANON),
    ("ttls", "mschapv2", None),
])
def test_enterprise_args_always_validate_the_server(method, inner, anon):
    from app.core.network import enterprise_args
    args = enterprise_args(method=method, inner_method=inner, identity=IDENTITY,
                           anonymous_identity=anon, server_name="radius.example.edu")
    pairs = dict(zip(args[::2], args[1::2]))
    assert pairs["802-1x.system-ca-certs"] == "yes"
    assert pairs["802-1x.domain-suffix-match"] == "radius.example.edu"
    assert pairs["wifi-sec.key-mgmt"] == "wpa-eap"
    assert pairs["802-1x.eap"] == method
    assert pairs["802-1x.phase2-auth"] == (inner or ("mschapv2" if method == "peap" else "pap"))
    assert ("802-1x.anonymous-identity" in pairs) == (anon is not None)
    assert "802-1x.password" not in pairs


@pytest.mark.unit
@pytest.mark.parametrize("server_name", [None, "", "   ", "localhost", "bad name.edu"])
def test_enterprise_args_refuse_without_a_valid_server_name(server_name):
    from app.core.network import enterprise_args
    with pytest.raises(ValueError) as exc:
        enterprise_args(method="peap", inner_method=None, identity=IDENTITY,
                        anonymous_identity=None, server_name=server_name)
    assert IDENTITY not in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("fields, codes", [
    (dict(ssid="", security="open"), ["ssid_invalid"]),
    (dict(ssid="x" * 33, security="open"), ["ssid_invalid"]),
    (dict(ssid="é" * 17, security="open"), ["ssid_invalid"]),
    (dict(ssid="x" * 32, security="open"), []),
    (dict(ssid="Home", security="password"), ["password_required"]),
    (dict(ssid="Home", security="password", password="short"), ["password_length"]),
    (dict(ssid="Home", security="password", password="x" * 64), ["password_length"]),
    (dict(ssid="Home", security="password", password="x" * 63), []),
    (dict(ssid="eduroam", security="enterprise", method="peap", serverName="radius.example.edu"),
     ["password_required", "identity_required"]),
    (dict(ssid="eduroam", security="enterprise", method="peap", identity=IDENTITY, password=PASSWORD),
     ["server_name_required"]),
    (dict(ssid="eduroam", security="enterprise", method="peap", identity=IDENTITY, password=PASSWORD,
          serverName="radius"), ["server_name_invalid"]),
    (dict(ssid="eduroam", security="enterprise", method="peap", identity=IDENTITY, password=PASSWORD,
          serverName="radius.example.edu/x"), ["server_name_invalid"]),
    (dict(ssid="eduroam", security="enterprise", method="peap", innerMethod="pap", identity=IDENTITY,
          password=PASSWORD, serverName="radius.example.edu"), ["inner_method_invalid"]),
    (dict(ssid="eduroam", security="enterprise", method="ttls", innerMethod="mschapv2", identity=IDENTITY,
          password=PASSWORD, serverName="radius.example.edu"), []),
])
def test_join_problems(fields, codes):
    from app.core.network import join_problems
    kwargs = dict(password=None, identity=None, method=None, innerMethod=None, serverName=None)
    kwargs.update(fields)
    assert join_problems(ssid=kwargs["ssid"], security=kwargs["security"], password=kwargs["password"],
                         identity=kwargs["identity"], method=kwargs["method"],
                         inner_method=kwargs["innerMethod"], server_name=kwargs["serverName"]) == codes


@pytest.mark.unit
def test_claim_join_refuses_a_second_join(nm):
    from app.core import network
    assert network.claim_join(ssid="Captua Lab", security="open") == "wlan0"
    assert network.join_state()["state"] == "joining"
    with pytest.raises(network.NetworkRefused) as exc:
        network.claim_join(ssid="Other", security="open")
    assert exc.value.codes == ["join_in_progress"]
    detail = exc.value.detail()
    assert detail["problems"][0]["code"] == "join_in_progress" and detail["problems"][0]["message"]


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def _join(**overrides):
    from app.core import network
    kwargs = dict(device="wlan0", ssid="Captua Lab", security="password", password=PASSWORD)
    kwargs.update(overrides)
    return network.join(**kwargs)


@pytest.mark.unit
def test_join_success_removes_other_profiles_for_the_ssid_only(nm):
    _join_nm(nm)
    nm.on(CONNECTIONS, stdout=fixture("nmcli-connection-show.txt")
          + f"Captua Lab:{NEW_UUID}:802-11-wireless:yes\n")
    nm.on(f"{DETAILS} {NEW_UUID}", stdout=details("Captua Lab", "wpa-psk"))
    outcome = _join()
    assert outcome.joined and outcome.problem is None and outcome.restored is None
    assert [c[-1] for c in nm.ran(DELETE)] == [OLD_UUID]
    add = nm.ran(ADD)[0]
    assert add == ["nmcli", "connection", "add", "type", "wifi", "con-name", "Captua Lab",
                   "ifname", "wlan0", "ssid", "Captua Lab", "connection.uuid", NEW_UUID,
                   "wifi-sec.key-mgmt", "wpa-psk"]
    up = nm.child(UP_NEW)[0]
    assert up.argv == ["nmcli", "--wait", "45", "connection", "up", "uuid", NEW_UUID, "ifname", "wlan0",
                       "passwd-file", "/dev/stdin"]
    assert up.input == f"802-11-wireless-security.psk:{PASSWORD}\n"
    assert nm.kwargs[nm.calls.index(up.argv)]["stdin"] is subprocess.PIPE
    assert not nm.ran(UP_OLD)


@pytest.mark.unit
def test_join_open_network_has_no_security_arguments(nm):
    _join_nm(nm)
    _join(ssid="Library Guest", security="open", password=None)
    assert nm.ran(ADD)[0][9:] == ["ssid", "Library Guest", "connection.uuid", NEW_UUID]
    up = nm.ran(UP_NEW)[0]
    assert "passwd-file" not in up
    assert nm.kwargs[nm.calls.index(up)]["stdin"] is subprocess.DEVNULL
    assert nm.inputs() == []


@pytest.mark.unit
def test_join_uses_sae_for_a_wpa3_only_network(nm):
    _join_nm(nm)
    _join(ssid="NewHome")
    assert nm.ran(ADD)[0][13:] == ["wifi-sec.key-mgmt", "sae"]
    assert nm.inputs() == [f"802-11-wireless-security.psk:{PASSWORD}\n"]


@pytest.mark.unit
def test_join_enterprise_profile(nm):
    _join_nm(nm)
    _join(ssid="eduroam", security="enterprise", identity=IDENTITY, method="peap",
          anonymous_identity=ANON, server_name="radius.example.edu")
    args = nm.ran(ADD)[0][13:]
    pairs = dict(zip(args[::2], args[1::2]))
    assert pairs == {
        "wifi-sec.key-mgmt": "wpa-eap", "802-1x.eap": "peap", "802-1x.phase2-auth": "mschapv2",
        "802-1x.identity": IDENTITY, "802-1x.anonymous-identity": ANON,
        "802-1x.system-ca-certs": "yes", "802-1x.domain-suffix-match": "radius.example.edu",
    }
    assert nm.inputs() == [f"802-1x.identity:{IDENTITY}\n802-1x.password:{PASSWORD}\n"]


@pytest.mark.unit
def test_join_failure_ends_early_on_a_decisive_line(nm):
    _join_nm(nm, up_rc=4, up_waits=None)
    nm.on(JOURNAL, stdout=_journal_after(nm, 3, journal_for("journal-server-name-mismatch.txt", ssid="eduroam")))
    start = nm.clock.t
    outcome = _join(ssid="eduroam", security="enterprise", identity=IDENTITY, method="peap",
                    server_name="other.example.org")
    assert not outcome.joined
    assert outcome.problem == {"code": "server_name_mismatch", "detail": None, "server": SERVER}
    up = nm.child(UP_NEW)[0]
    assert up.terminated
    assert nm.clock.t - start < 10
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]
    assert nm.ran(UP_OLD) and outcome.restored is True
    since = nm.ran(JOURNAL)[0]
    assert since == ["journalctl", "-t", "wpa_supplicant", "--since", "@1794419003", "-o", "short-unix",
                     "--no-pager"]


@pytest.mark.unit
def test_join_failure_restore_failed(nm):
    _join_nm(nm, up_rc=4, up_waits=0)
    nm.on(JOURNAL, stdout=journal_for("journal-wrong-password.txt", ssid="eduroam"))
    nm.on(UP_OLD, rc=4, stderr="Error: Connection activation failed")
    outcome = _join(ssid="eduroam", security="enterprise", identity=IDENTITY, method="ttls",
                    server_name="radius.example.edu")
    assert outcome.problem["code"] == "wrong_credentials"
    assert outcome.restored is False
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]


@pytest.mark.unit
def test_join_failure_without_a_previous_connection(nm):
    _join_nm(nm, up_rc=4, up_waits=0)
    nm.on(CON_UUID, stdout="GENERAL.CON-UUID:\n")
    outcome = _join()
    assert outcome.problem == {"code": "failed", "detail": None, "server": None}
    assert outcome.restored is None
    assert not nm.ran("nmcli --wait 30")
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]


@pytest.mark.unit
@pytest.mark.parametrize("up, code", [
    (dict(up_rc=3, up_waits=0, up_stderr="Error: Timeout expired (45 seconds)"), "timeout"),
    (dict(up_rc=4, up_waits=0, up_stderr="Error: Not authorized to control networking."), "not_permitted"),
    (dict(up_rc=4, up_waits=0, up_stderr="Error: insufficient privileges"), "not_permitted"),
    (dict(up_rc=4, up_waits=0, up_stderr="Error: Connection activation failed: Secrets were required, but not provided"), "failed"),
])
def test_join_failure_without_a_decisive_line(nm, up, code):
    _join_nm(nm, **up)
    assert _join().problem["code"] == code


@pytest.mark.unit
def test_join_that_outlives_its_wait_is_stopped_as_timeout(nm):
    _join_nm(nm, up_waits=None)
    outcome = _join()
    assert outcome.problem["code"] == "timeout"
    assert nm.child(UP_NEW)[0].killed or nm.child(UP_NEW)[0].terminated
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]
    assert outcome.restored is True


@pytest.mark.unit
def test_join_deletes_the_new_profile_when_an_exception_is_raised(nm):
    _join_nm(nm)
    nm.on(UP_NEW, raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        _join()
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]
    assert nm.ran(UP_OLD)


@pytest.mark.unit
def test_run_join_records_an_exception_as_failed(nm):
    from app.core import network
    _join_nm(nm)
    nm.on(UP_NEW, raises=RuntimeError("boom"))
    network.claim_join(ssid="Captua Lab", security="password", password=PASSWORD)
    network.run_join(device="wlan0", ssid="Captua Lab", security="password", password=PASSWORD)
    state = network.join_state()
    assert state["state"] == "failed" and state["problem"]["code"] == "failed"
    assert state["finishedAt"] is not None
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]


@pytest.mark.unit
@pytest.mark.parametrize("journal", [dict(raises=FileNotFoundError("journalctl")), dict(rc=1, stderr="No journal files"),
                                     dict(waits=None)])
def test_join_without_a_journal_still_reports(nm, journal):
    _join_nm(nm, up_rc=4, up_waits=3)
    nm.on(JOURNAL, **journal)
    outcome = _join()
    assert outcome.problem["code"] == "failed"
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]


@pytest.mark.unit
def test_join_reads_the_journal_once_more_after_nmcli_ends(nm):
    _join_nm(nm, up_rc=4, up_waits=0)
    nm.on(JOURNAL, stdout=journal_for("journal-untrusted-ca.txt"))
    assert _join().problem["code"] == "server_not_trusted"


@pytest.mark.unit
def test_join_add_failure(nm):
    _join_nm(nm)
    nm.on(ADD, rc=2, stderr=f"Error: invalid psk {PASSWORD}")
    outcome = _join()
    assert outcome.problem["code"] == "failed"
    assert not nm.ran(UP_NEW) and not nm.ran(DELETE)


@pytest.mark.unit
def test_join_commands_that_scan_activate_or_restore_carry_ifname(nm):
    _join_nm(nm, up_rc=4, up_waits=1)
    _join()
    acting = [c for c in nm.calls if "up" in c or "add" in c or ("wifi" in c and "list" in c)]
    assert len(acting) >= 4
    for argv in acting:
        assert argv[argv.index("ifname") + 1] == "wlan0"


# ---------------------------------------------------------------------------
# The password stays out of logs, exceptions and state
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("path", ["success", "failure", "timeout", "add_oserror", "up_oserror", "add_timeout"])
def test_password_appears_nowhere(nm, caplog, path):
    from app.core import network
    _join_nm(nm)
    if path == "failure":
        _join_nm(nm, up_rc=4, up_waits=0, up_stderr=f"Error: secret {PASSWORD} rejected")
        nm.on(JOURNAL, stdout=journal_for("journal-wrong-password.txt", ssid="eduroam"))
    elif path == "timeout":
        _join_nm(nm, up_waits=None)
    elif path == "add_oserror":
        nm.on(ADD, raises=OSError(f"exec failed: {PASSWORD}"))
    elif path == "up_oserror":
        nm.on(UP_NEW, raises=OSError(f"exec failed: {PASSWORD}"))
    elif path == "add_timeout":
        nm.on(ADD, waits=None)
    caplog.set_level(logging.DEBUG)
    kwargs = dict(device="wlan0", ssid="eduroam", security="enterprise", password=PASSWORD,
                  identity=IDENTITY, method="peap", anonymous_identity=ANON, server_name="radius.example.edu")
    network.claim_join(**{k: v for k, v in kwargs.items() if k != "device"})
    texts = []
    try:
        network.run_join(**kwargs)
    except Exception as exc:  # run_join must not raise; if it does, its text is checked too
        texts.append(repr(exc))
    state = network.join_state()
    assert state["state"] in ("joined", "failed")
    texts += [caplog.text, repr(state), repr(network._join_state)]
    texts += [repr(r.args) + r.getMessage() for r in caplog.records]
    for text in texts:
        assert PASSWORD not in text
        assert IDENTITY not in text and ANON not in text


@pytest.mark.unit
def test_runner_errors_carry_no_argv(nm):
    from app.core import network
    nm.on("nmcli secret", raises=OSError(f"no such file: {PASSWORD}"))
    with pytest.raises(network.NetworkUnavailable) as exc:
        network._command(["nmcli", "secret", PASSWORD], timeout=5)
    assert PASSWORD not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    nm.on("nmcli secret", waits=None)
    with pytest.raises(network.NetworkUnavailable) as exc:
        network._command(["nmcli", "secret", PASSWORD], timeout=5)
    assert PASSWORD not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


# ---------------------------------------------------------------------------
# Forget
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_forget(nm):
    from app.core import network
    nm.on(DELETE, rc=0)
    with pytest.raises(network.SavedNotFound):
        network.forget("5f0e7a1c-3b2d-4e5f-8a9b-0c1d2e3f4a5b", force=False)
    with pytest.raises(network.NetworkRefused) as exc:
        network.forget(OLD_UUID, force=False)
    assert exc.value.codes == ["connection_active"]
    assert not nm.ran(DELETE)
    assert network.forget(OLD_UUID, force=True) == "Captua Lab"
    assert network.forget(EDUROAM_UUID, force=False) == "eduroam"
    assert [c[-1] for c in nm.ran(DELETE)] == [OLD_UUID, EDUROAM_UUID]


# ---------------------------------------------------------------------------
# Status: bench rows, every Wi-Fi device, IPv4 gateway, failed reads
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_status_leaves_out_the_bench_units_veth_rows(nm):
    """`nmcli device status` from the bench unit: Docker's veth devices are
    `ethernet`/`unmanaged` rows there, and stay out of the status."""
    from app.core import network
    nm.on(DEVICE_STATUS, stdout=fixture("nmcli-device-status-bench.txt"))
    nm.on("nmcli -t -f GENERAL.HWADDR", stdout="")
    body = network.status()
    assert [(i["device"], i["type"]) for i in body["interfaces"]] == [("wlan0", "wifi"), ("eth0", "ethernet")]
    assert not [c for c in nm.calls if any(a.startswith("veth") for a in c)]


@pytest.mark.unit
def test_status_reads_ssid_and_signal_for_every_connected_wifi_device(nm):
    from app.core import network
    nm.on(DEVICE_STATUS, stdout="wlan0:wifi:connected:Captua Lab\nwlan1:wifi:connected:Annex\n"
                                "wlan2:wifi:disconnected:\n")
    nm.on("nmcli -t -f GENERAL.HWADDR", stdout="IP4.ADDRESS[1]:10.0.0.5/24\nIP4.GATEWAY:10.0.0.1\nIP4.DNS[1]:10.0.0.1\n")
    nm.on("nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list ifname wlan1 --rescan no",
          stdout=" :Captua Lab:30:WPA2\n*:Annex:58:WPA2\n")
    body = network.status()
    seen = {i["device"]: (i["ssid"], i["signal"]) for i in body["interfaces"]}
    assert seen == {"wlan0": ("Captua Lab", 72), "wlan1": ("Annex", 58), "wlan2": (None, None)}
    listed = [c[c.index("ifname") + 1] for c in nm.ran("nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY")]
    assert listed == ["wlan0", "wlan1"]
    assert body["wifiDevice"] == "wlan0"


@pytest.mark.unit
def test_status_gateway_is_ipv4_and_dns_lists_ipv4_then_ipv6(nm):
    from app.core import network
    nm.on(DEVICE_STATUS, stdout="eth0:ethernet:connected:Wired connection 1\n")
    nm.on("nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show eth0",
          stdout="IP6.DNS[1]:fd00::1\nIP4.ADDRESS[1]:10.0.0.5/24\nIP6.GATEWAY:fe80::1\nIP4.GATEWAY:\n"
                 "IP4.DNS[1]:10.0.0.1\nIP6.DNS[2]:fd00::2\nIP4.DNS[2]:10.0.0.2\n")
    eth0 = network.status()["interfaces"][0]
    assert eth0["gateway"] is None
    assert eth0["dns"] == ["10.0.0.1", "10.0.0.2", "fd00::1", "fd00::2"]


@pytest.mark.unit
@pytest.mark.parametrize("failing", [
    DEVICE_STATUS, PERMISSIONS, SHOW_WLAN0, WIFI_LIST_CACHED,
    "nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show eth0",
])
def test_any_failed_status_read_raises_unavailable(nm, failing):
    from app.core import network
    nm.on("nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show eth0", stdout="")
    nm.on(failing, rc=8, stderr="Error: NetworkManager is not running.")
    with pytest.raises(network.NetworkUnavailable):
        network.status()


@pytest.mark.unit
def test_a_configured_wifi_device_absent_from_device_status_is_no_wifi_device(nm, monkeypatch):
    from app.core import config, network
    monkeypatch.setattr(config.settings, "WIFI_DEVICE", "wlan9", raising=False)
    assert network.wifi_device() is None
    with pytest.raises(network.NetworkRefused) as exc:
        network.scan()
    assert exc.value.codes == ["no_wifi_device"]
    with pytest.raises(network.NetworkRefused) as exc:
        network.claim_join(ssid="Captua Lab", security="open")
    assert exc.value.codes == ["no_wifi_device"]
    nm.on("nmcli -t -f GENERAL.HWADDR", stdout="")
    assert network.status()["wifiDevice"] is None
    assert not nm.ran("nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list ifname wlan9")


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def _permissions_with(**values):
    text = fixture("nmcli-general-permissions-pi-rule-installed.txt")
    names = {"scan": "wifi.scan", "control": "network-control", "modify": "settings.modify.system"}
    for key, value in values.items():
        text = text.replace(f"org.freedesktop.NetworkManager.{names[key]}:yes",
                            f"org.freedesktop.NetworkManager.{names[key]}:{value}")
    return text


@pytest.mark.unit
@pytest.mark.parametrize("values, expected", [
    ({}, {"scan": True, "control": True, "modify": True}),
    ({"scan": "no"}, {"scan": False, "control": True, "modify": True}),
    ({"control": "auth"}, {"scan": True, "control": False, "modify": True}),
    ({"modify": "auth", "scan": "auth"}, {"scan": False, "control": True, "modify": False}),
])
def test_permissions(nm, values, expected):
    from app.core import network
    nm.on(PERMISSIONS, stdout=_permissions_with(**values))
    assert network.permissions() == expected
    assert nm.ran(PERMISSIONS)[0] == ["nmcli", "-t", "general", "permissions"]


@pytest.mark.unit
def test_permissions_missing_from_the_answer_are_false(nm):
    from app.core import network
    nm.on(PERMISSIONS, stdout="org.freedesktop.NetworkManager.wifi.scan:yes\n")
    assert network.permissions() == {"scan": True, "control": False, "modify": False}


@pytest.mark.unit
@pytest.mark.parametrize("rescan", [False, True])
def test_scan_without_the_scan_permission_lists_the_cached_networks(nm, rescan):
    """No wifi.scan permission: never refused; the cached list, whatever was asked."""
    from app.core import network
    nm.on(PERMISSIONS, stdout=_permissions_with(scan="no"))
    body = network.scan(rescan=rescan)
    assert body["rescanned"] is False
    assert body["networks"][0]["ssid"] == "Captua Lab"
    listed = nm.ran("nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list")
    assert listed == [WIFI_LIST_CACHED.split(" ")]


@pytest.mark.unit
def test_scan_reports_whether_it_rescanned(nm):
    from app.core import network
    nm.on(WIFI_LIST_RESCAN, stdout=fixture("nmcli-wifi-list.txt"))
    assert network.scan()["rescanned"] is False
    assert network.scan(rescan=True)["rescanned"] is True


@pytest.mark.unit
@pytest.mark.parametrize("values", [{"control": "no"}, {"modify": "auth"}, {"control": "auth", "modify": "no"}])
def test_join_without_control_or_modify_is_not_permitted_before_anything_runs(nm, values):
    from app.core import network
    nm.on(PERMISSIONS, stdout=_permissions_with(**values))
    with pytest.raises(network.NetworkRefused) as exc:
        network.claim_join(ssid="Captua Lab", security="password", password=PASSWORD)
    assert exc.value.codes == ["not_permitted"]
    assert exc.value.detail()["problems"][0]["message"]
    assert network.join_state()["state"] == "idle"
    assert not nm.ran(ADD) and not nm.ran("nmcli --wait")


# ---------------------------------------------------------------------------
# Scan: rescan on request
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_scan_rescans_only_when_asked(nm):
    from app.core import network
    nm.on(WIFI_LIST_RESCAN, stdout=fixture("nmcli-wifi-list.txt"))
    network.scan()
    assert nm.ran(WIFI_LIST_SCAN) and not nm.ran(WIFI_LIST_RESCAN)
    network.scan(rescan=True)
    assert len(nm.ran(WIFI_LIST_RESCAN)) == 1


@pytest.mark.unit
@pytest.mark.parametrize("rescan, listed", [(False, WIFI_LIST_SCAN), (True, WIFI_LIST_RESCAN)])
def test_scan_allows_twenty_seconds(nm, rescan, listed):
    from app.core import network
    nm.on(listed, waits=None)
    start = nm.clock.t
    with pytest.raises(network.NetworkUnavailable):
        network.scan(rescan=rescan)
    assert nm.clock.t - start == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Journal: the bench captures of joins whose secret went through stdin
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_classify_wrong_psk_from_the_bench():
    from app.core.network import classify
    lines = fixture("journal-psk-wrong-key.txt").splitlines()
    assert classify(lines, "wlan6", "captua-B") == {"code": "wrong_password", "detail": None, "server": None}
    # One 4-Way Handshake line, or one attempt (4-Way plus WRONG_KEY), is not
    # decisive: a reason-15 disconnect at weak signal prints the same line.
    first = lines[: next(i for i, l in enumerate(lines) if "4-Way Handshake failed" in l) + 1]
    one_attempt = lines[: next(i for i, l in enumerate(lines) if "reason=WRONG_KEY" in l) + 1]
    assert classify(first, "wlan6", "captua-B") is None
    assert classify(one_attempt, "wlan6", "captua-B") is None
    assert classify(one_attempt, "wlan6", "captua-B", final=True)["code"] == "wrong_password"
    # The second 4-Way Handshake failure decides.
    second = [i for i, l in enumerate(lines) if "4-Way Handshake failed" in l][1]
    assert classify(lines[: second + 1], "wlan6", "captua-B")["code"] == "wrong_password"
    assert classify(lines[:second], "wlan6", "captua-B") is None
    assert classify(lines, "wlan2", "captua-B") is None
    assert classify(lines, "wlan6", "captua-A") is None


@pytest.mark.unit
def test_classify_wrong_eap_password_from_the_bench():
    from app.core.network import classify
    lines = fixture("journal-eap-wrong-password-stdin.txt").splitlines()
    assert classify(lines, "wlan2", "captua-E") == {"code": "wrong_credentials", "detail": None, "server": None}
    # The first MSCHAPv2 error 691 line decides at once.
    first = lines[: next(i for i, l in enumerate(lines) if "error 691" in l) + 1]
    assert not any("EAP-FAILURE" in l for l in first)
    assert classify(first, "wlan2", "captua-E")["code"] == "wrong_credentials"
    assert classify(lines, "wlan6", "captua-E") is None


@pytest.mark.unit
def test_classify_auth_failed_or_eap_failure_decides_on_the_second():
    from app.core.network import classify
    lines = fixture("journal-eap-wrong-password-stdin.txt").splitlines()
    anchor = next(l for l in lines if "Trying to authenticate" in l)
    for marker in ("reason=AUTH_FAILED", "CTRL-EVENT-EAP-FAILURE"):
        line = next(l for l in lines if marker in l)
        assert classify([line], "wlan2", "captua-E", final=True) is None      # no anchor yet
        assert classify([anchor, line], "wlan2", "captua-E") is None
        assert classify([anchor, line], "wlan2", "captua-E", final=True) == {
            "code": "wrong_credentials", "detail": None, "server": None}
        assert classify([anchor, line, line], "wlan2", "captua-E")["code"] == "wrong_credentials"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["journal-sae-joined.txt", "journal-not-in-range.txt"])
def test_classify_finds_nothing_in_a_join_or_a_network_out_of_range(name):
    from app.core.network import classify
    assert classify(fixture(name).splitlines(), "wlan6", "captua-S") is None
    assert classify(fixture(name).splitlines(), "wlan6", "captua-S", final=True) is None


@pytest.mark.unit
def test_join_wrong_psk_stops_nmcli_and_deletes_the_profile(nm):
    """On the bench nmcli waits out its 45 s (exit 3) while NetworkManager
    retries; the journal decides within seconds instead."""
    _join_nm(nm, up_rc=3, up_waits=None, up_stderr="Error: Timeout expired (45 seconds)")
    nm.on(JOURNAL, stdout=_journal_after(nm, 2, journal_for("journal-psk-wrong-key.txt")))
    start = nm.clock.t
    outcome = _join(ssid="Captua Lab")
    assert outcome.problem == {"code": "wrong_password", "detail": None, "server": None}
    assert nm.child(UP_NEW)[0].terminated
    assert nm.clock.t - start < 10
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]


@pytest.mark.unit
def test_join_wrong_eap_password_stops_nmcli_and_deletes_the_profile(nm):
    _join_nm(nm, up_rc=3, up_waits=None)
    nm.on(JOURNAL, stdout=_journal_after(nm, 1, journal_for("journal-eap-wrong-password-stdin.txt", ssid="eduroam")))
    start = nm.clock.t
    outcome = _join(ssid="eduroam", security="enterprise", identity=IDENTITY, method="peap",
                    server_name="radius.example.edu")
    assert outcome.problem["code"] == "wrong_credentials"
    assert nm.child(UP_NEW)[0].terminated
    assert nm.clock.t - start < 10
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]


# ---------------------------------------------------------------------------
# Join: the generated UUID and `restored`
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_join_uses_the_uuid_it_generated(nm, monkeypatch):
    from app.core import network
    monkeypatch.setattr(network, "_uuid4", uuid.uuid4)
    _join_nm(nm, up_rc=4, up_waits=0)
    nm.on("nmcli --wait 45 connection up uuid", rc=4)
    _join()
    add = nm.ran(ADD)[0]
    made = add[add.index("connection.uuid") + 1]
    assert str(uuid.UUID(made)) == made and made != NEW_UUID
    assert nm.ran("nmcli --wait 45 connection up uuid")[0][6] == made
    assert [c[-1] for c in nm.ran(DELETE)] == [made]


@pytest.mark.unit
def test_join_add_timeout_deletes_the_generated_uuid(nm):
    _join_nm(nm)
    nm.on(ADD, waits=None)
    outcome = _join()
    assert outcome.problem["code"] == "failed"
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]
    assert not nm.ran(UP_NEW)


@pytest.mark.unit
def test_restored_when_networkmanager_reconnected_by_itself(nm):
    _join_nm(nm, up_rc=4, up_waits=0)
    nm.active_after = OLD_UUID
    outcome = _join()
    assert outcome.restored is True
    assert not nm.ran(UP_OLD)
    order = [" ".join(c) for c in nm.calls]
    reads = [i for i, c in enumerate(order) if c.startswith(CON_UUID)]
    assert len(reads) == 2 and reads[1] > order.index(f"{DELETE} {NEW_UUID}")


@pytest.mark.unit
@pytest.mark.parametrize("after, up_rc, restored", [("", 0, True), ("", 4, False),
                                                     ("5f0e7a1c-3b2d-4e5f-8a9b-0c1d2e3f4a5b", 0, True)])
def test_restore_activates_the_previous_connection_when_it_is_not_active(nm, after, up_rc, restored):
    _join_nm(nm, up_rc=4, up_waits=0)
    nm.active_after = after
    nm.on(UP_OLD, rc=up_rc)
    outcome = _join()
    assert outcome.restored is restored
    up = nm.ran(UP_OLD)
    assert len(up) == 1 and nm.kwargs[nm.calls.index(up[0])]["stdin"] is subprocess.DEVNULL


@pytest.mark.unit
def test_restore_runs_up_when_the_active_connection_cannot_be_read(nm):
    _join_nm(nm, up_rc=4, up_waits=0)
    nm.on(CON_UUID, stdout=f"GENERAL.CON-UUID:{OLD_UUID}\n", rc=lambda n: 8 if n.ran(DELETE) else 0)
    assert _join().restored is True and nm.ran(UP_OLD)


# ---------------------------------------------------------------------------
# Secrets through stdin, never in argv
# ---------------------------------------------------------------------------

JOINS = [
    dict(ssid="Library Guest", security="open", password=None),
    dict(ssid="Captua Lab", security="password"),
    dict(ssid="NewHome", security="password"),
    dict(ssid="eduroam", security="enterprise", identity=IDENTITY, method="peap", server_name="radius.example.edu"),
    dict(ssid="eduroam", security="enterprise", identity=IDENTITY, method="peap", anonymous_identity=ANON,
         inner_method="mschapv2", server_name="radius.example.edu"),
    dict(ssid="eduroam", security="enterprise", identity=IDENTITY, method="ttls", server_name="radius.example.edu"),
    dict(ssid="eduroam", security="enterprise", identity=IDENTITY, method="ttls", inner_method="mschapv2",
         anonymous_identity=ANON, server_name="radius.example.edu"),
]

PATHS = {
    "joined": dict(),
    "decisive": dict(up_rc=3, up_waits=None),
    "timeout": dict(up_waits=None),
    "failed": dict(up_rc=4, up_waits=0, up_stderr="Error: Connection activation failed"),
    "not_permitted": dict(up_rc=4, up_waits=0, up_stderr="Error: Insufficient privileges"),
}


def _walk(nm, join, path, extra):
    _join_nm(nm, **PATHS[path])
    if path == "decisive":
        nm.on(JOURNAL, stdout=journal_for("journal-psk-wrong-key.txt", ssid=join["ssid"])
              + journal_for("journal-eap-wrong-password-stdin.txt", ssid=join["ssid"]))
    nm.on(f"{DETAILS} {NEW_UUID}", stdout=details(join["ssid"], ""))
    if extra == "add_fails":
        nm.on(ADD, rc=4, stderr="Error: Failed to add 'x' connection: Insufficient privileges")
    elif extra == "add_hangs":
        nm.on(ADD, waits=None)
    elif extra == "up_raises":
        nm.on(UP_NEW, raises=OSError("exec failed"))
    elif extra == "restore_fails":
        nm.on(UP_OLD, rc=4)
    try:
        _join(**join)
    except OSError:
        pass


@pytest.mark.unit
@pytest.mark.parametrize("join", JOINS, ids=lambda j: f"{j['security']}-{j.get('method') or j['ssid']}")
@pytest.mark.parametrize("path", list(PATHS))
@pytest.mark.parametrize("extra", [None, "add_fails", "add_hangs", "up_raises", "restore_fails"])
def test_no_argv_carries_the_password(nm, join, path, extra):
    """Every argv the join can produce: the password is in none of them. The
    identity is only the value of `802-1x.identity` in `connection add`."""
    _walk(nm, join, path, extra)
    assert nm.calls
    for argv in nm.calls:
        assert all(PASSWORD not in arg for arg in argv), argv[:4]
        if argv[:3] != ["nmcli", "connection", "add"]:
            assert all(IDENTITY not in arg for arg in argv), argv[:4]
        else:
            assert [i for i, a in enumerate(argv) if IDENTITY in a] == (
                [argv.index("802-1x.identity") + 1] if join["security"] == "enterprise" else [])
    # The password travels only on the stdin of the new profile's activation.
    for child in nm.children:
        if child.input is not None:
            assert " ".join(child.argv).startswith(UP_NEW_STDIN)
            assert child.argv[-2:] == ["passwd-file", "/dev/stdin"]
    expected = {"open": None, "password": f"802-11-wireless-security.psk:{PASSWORD}\n",
                "enterprise": f"802-1x.identity:{IDENTITY}\n802-1x.password:{PASSWORD}\n"}[join["security"]]
    ups = nm.child(UP_NEW)
    for up in ups:
        assert up.input == expected


@pytest.mark.unit
@pytest.mark.parametrize("fields, codes", [
    (dict(security="password", password="line\nbreak-1"), ["password_invalid"]),
    (dict(security="password", password="carriage\rret"), ["password_invalid"]),
    (dict(security="enterprise", password="nul\x00here", identity=IDENTITY), ["password_invalid"]),
    (dict(security="enterprise", password=PASSWORD, identity="a@b.edu\n802-1x.password:x"), ["identity_invalid"]),
])
def test_join_refuses_line_breaks_in_secrets(fields, codes):
    from app.core.network import join_problems
    kwargs = dict(identity=None)
    kwargs.update(fields)
    assert join_problems(ssid="eduroam", method="peap", inner_method=None, server_name="radius.example.edu",
                         **kwargs) == codes


@pytest.mark.unit
@pytest.mark.parametrize("password, identity", [("a\nb-cdefgh", IDENTITY), (PASSWORD, "x\ny")])
def test_secrets_text_refuses_line_breaks(password, identity):
    from app.core.network import _secrets_text
    with pytest.raises(ValueError) as exc:
        _secrets_text(security="enterprise", password=password, identity=identity)
    assert password not in str(exc.value) and identity not in str(exc.value)


@pytest.mark.unit
def test_runner_input_errors_carry_no_secret(nm):
    from app.core import network
    nm.on("nmcli secret", waits=None)
    with pytest.raises(network.NetworkUnavailable) as exc:
        network._command(["nmcli", "secret"], timeout=5, input_text=f"x:{PASSWORD}\n")
    assert PASSWORD not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert nm.child("nmcli secret")[0].input == f"x:{PASSWORD}\n"


# ---------------------------------------------------------------------------
# Forget: refused by polkit
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_forget_refused_by_polkit_is_not_permitted(nm):
    from app.core import network
    nm.on(DELETE, rc=7, stderr=fixture("nmcli-delete-denied.txt"))
    with pytest.raises(network.NetworkRefused) as exc:
        network.forget(EDUROAM_UUID, force=False)
    assert exc.value.codes == ["not_permitted"]


@pytest.mark.unit
def test_forget_other_failure_is_unavailable(nm):
    from app.core import network
    nm.on(DELETE, rc=10, stderr="Error: unknown connection")
    with pytest.raises(network.NetworkUnavailable):
        network.forget(EDUROAM_UUID, force=False)



# ---------------------------------------------------------------------------
# Second review round
# ---------------------------------------------------------------------------

PREVIOUS_NET_FAILURES = [
    # Lines of the connection that was active before, inside the journal window.
    "wlan0: CTRL-EVENT-DISCONNECTED bssid=02:00:00:00:00:00 reason=3 locally_generated=1",
    "wlan0: WPA: 4-Way Handshake failed - pre-shared key may be incorrect",
    'wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid="Previous Net" auth_failures=1 duration=10 reason=WRONG_KEY',
    "wlan0: WPA: 4-Way Handshake failed - pre-shared key may be incorrect",
    'wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid="Previous Net" auth_failures=2 duration=20 reason=WRONG_KEY',
]
JOINED_LINES = [
    "wlan0: SME: Trying to authenticate with 02:00:00:00:01:00 (SSID='Captua Lab' freq=2412 MHz)",
    "wlan0: Trying to associate with 02:00:00:00:01:00 (SSID='Captua Lab' freq=2412 MHz)",
    "wlan0: Associated with 02:00:00:00:01:00",
] + [l.replace("wlan6", "wlan0") for l in fixture("journal-sae-joined.txt").splitlines()]


@pytest.mark.unit
def test_lines_before_the_anchor_are_not_this_joins():
    from app.core.network import classify
    assert classify(PREVIOUS_NET_FAILURES, "wlan0", "Captua Lab") is None
    assert classify(PREVIOUS_NET_FAILURES, "wlan0", "Captua Lab", final=True) is None
    assert classify(PREVIOUS_NET_FAILURES + JOINED_LINES, "wlan0", "Captua Lab", final=True) is None


@pytest.mark.unit
def test_the_anchor_is_an_attempt_on_the_target_ssid():
    from app.core.network import classify
    lines = journal_for("journal-psk-wrong-key.txt", ssid="Previous Net").splitlines()
    assert classify(lines, "wlan0", "Captua Lab", final=True) is None
    assert classify(lines, "wlan0", "Previous Net")["code"] == "wrong_password"
    # Either anchor form starts the window.
    for anchor in JOINED_LINES[:2]:
        assert classify([anchor] + PREVIOUS_NET_FAILURES[1:2] * 2, "wlan0", "Captua Lab")["code"] == "wrong_password"


@pytest.mark.unit
def test_temp_disabled_for_another_network_after_the_anchor_is_ignored():
    from app.core.network import classify
    lines = JOINED_LINES[:1] + [PREVIOUS_NET_FAILURES[2], PREVIOUS_NET_FAILURES[4]]
    assert classify(lines, "wlan0", "Captua Lab", final=True) is None


@pytest.mark.unit
def test_the_anchor_matches_the_whole_ssid():
    from app.core.network import classify
    lines = ["wlan0: Trying to associate with 02:00:00:00:01:00 (SSID='Captua Lab 2' freq=2412 MHz)",
             "wlan0: EAP-MSCHAPV2: failure message: 'FAILED' (retry not allowed, error 691)"]
    assert classify(lines, "wlan0", "Captua Lab") is None
    assert classify(lines, "wlan0", "Captua Lab 2")["code"] == "wrong_credentials"


@pytest.mark.unit
@pytest.mark.parametrize("name, ssid, code", [
    ("journal-server-name-mismatch.txt", "captua-eap-test", "server_name_mismatch"),
    ("journal-untrusted-ca.txt", "captua-eap-test", "server_not_trusted"),
    ("journal-ca-not-in-system-store.txt", "captua-eap-test", "server_not_trusted"),
    ("journal-wrong-password.txt", "captua-eap-test", "wrong_credentials"),
    ("journal-eap-wrong-password-stdin.txt", "captua-E", "wrong_credentials"),
])
def test_definitive_lines_decide_at_their_first_occurrence(name, ssid, code):
    """Certificate errors and MSCHAPv2 error 691: the first one stops the join."""
    from app.core.network import classify
    lines = fixture(name).splitlines()
    marker = "CTRL-EVENT-EAP-TLS-CERT-ERROR" if code.startswith("server") else "error 691"
    first = next(i for i, l in enumerate(lines) if marker in l)
    assert classify(lines[: first + 1], CAPTURED[name][0], ssid)["code"] == code
    assert classify(lines[:first], CAPTURED[name][0], ssid) is None


@pytest.mark.unit
def test_a_failure_then_connected_is_not_stopped():
    from app.core.network import classify
    lines = journal_for("journal-psk-wrong-key.txt").splitlines()
    one_attempt = lines[: next(i for i, l in enumerate(lines) if "reason=WRONG_KEY" in l) + 1]
    sequence = one_attempt + JOINED_LINES
    assert classify(sequence, "wlan0", "Captua Lab") is None
    assert classify(sequence, "wlan0", "Captua Lab", final=True) is None
    # A failure after the connection is counted afresh.
    assert classify(sequence + one_attempt[-3:-1], "wlan0", "Captua Lab") is None


@pytest.mark.unit
def test_join_with_the_previous_networks_failures_in_the_window_still_joins(nm):
    _join_nm(nm, up_rc=0, up_waits=3)
    nm.on(JOURNAL, stdout="\n".join(PREVIOUS_NET_FAILURES + JOINED_LINES) + "\n")
    outcome = _join(ssid="Captua Lab")
    assert outcome.joined and outcome.problem is None
    assert not nm.child(UP_NEW)[0].terminated
    assert NEW_UUID not in [c[-1] for c in nm.ran(DELETE)]


@pytest.mark.unit
def test_join_failure_then_connected_still_joins(nm):
    lines = journal_for("journal-psk-wrong-key.txt").splitlines()
    one_attempt = lines[: next(i for i, l in enumerate(lines) if "reason=WRONG_KEY" in l) + 1]
    _join_nm(nm, up_rc=0, up_waits=3)
    nm.on(JOURNAL, stdout="\n".join(one_attempt + JOINED_LINES) + "\n")
    outcome = _join(ssid="Captua Lab")
    assert outcome.joined and not nm.child(UP_NEW)[0].terminated


@pytest.mark.unit
def test_join_with_one_soft_line_runs_to_nmclis_timeout_then_classifies_it(nm):
    lines = journal_for("journal-psk-wrong-key.txt").splitlines()
    one_attempt = lines[: next(i for i, l in enumerate(lines) if "reason=WRONG_KEY" in l) + 1]
    _join_nm(nm, up_rc=3, up_waits=5, up_stderr="Error: Timeout expired (45 seconds)")
    nm.on(JOURNAL, stdout="\n".join(one_attempt) + "\n")
    outcome = _join(ssid="Captua Lab")
    up = nm.child(UP_NEW)[0]
    assert not up.terminated and up.returncode == 3
    assert outcome.problem == {"code": "wrong_password", "detail": None, "server": None}
    assert [c[-1] for c in nm.ran(DELETE)] == [NEW_UUID]


# SSIDs are compared exactly ------------------------------------------------

SPACED_UUID = "7e1f0c3a-2b4d-4c5e-9f6a-8b7c6d5e4f3a"


def _with_spaced_profile(nm):
    nm.on(CONNECTIONS, stdout=fixture("nmcli-connection-show.txt")
          + f" Captua Lab :{SPACED_UUID}:802-11-wireless:no\n"
          + f"Captua Lab:{NEW_UUID}:802-11-wireless:yes\n")
    nm.on(f"{DETAILS} {SPACED_UUID}", stdout=details(" Captua Lab ", "wpa-psk"))
    nm.on(f"{DETAILS} {NEW_UUID}", stdout=details("Captua Lab", "wpa-psk"))


@pytest.mark.unit
def test_joining_an_ssid_keeps_a_profile_whose_ssid_has_spaces(nm):
    _join_nm(nm)
    _with_spaced_profile(nm)
    assert _join(ssid="Captua Lab").joined
    assert [c[-1] for c in nm.ran(DELETE)] == [OLD_UUID]


@pytest.mark.unit
def test_joining_an_ssid_with_spaces_removes_its_own_older_profile_only(nm):
    _join_nm(nm)
    _with_spaced_profile(nm)
    nm.on(f"{DETAILS} {NEW_UUID}", stdout=details(" Captua Lab ", "wpa-psk"))
    nm.on(CONNECTIONS, stdout=fixture("nmcli-connection-show.txt")
          + f" Captua Lab :{SPACED_UUID}:802-11-wireless:no\n"
          + f" Captua Lab :{NEW_UUID}:802-11-wireless:yes\n")
    assert _join(ssid=" Captua Lab ").joined
    assert [c[-1] for c in nm.ran(DELETE)] == [SPACED_UUID]


@pytest.mark.unit
def test_saved_ssids_keep_their_spaces(nm):
    from app.core import network
    _with_spaced_profile(nm)
    saved = {s["uuid"]: s["ssid"] for s in network.saved_networks()}
    assert saved[SPACED_UUID] == " Captua Lab " and saved[OLD_UUID] == "Captua Lab"


@pytest.mark.unit
def test_scan_saved_flags_compare_ssids_exactly(nm):
    from app.core import network
    nm.on(WIFI_LIST_SCAN, stdout=fixture("nmcli-wifi-list.txt") + " : Captua Lab :50:WPA2\n :eduroam :33:WPA2\n")
    nm.on(CONNECTIONS, stdout=fixture("nmcli-connection-show.txt") + f" Captua Lab :{SPACED_UUID}:802-11-wireless:no\n")
    nm.on(f"{DETAILS} {SPACED_UUID}", stdout=details(" Captua Lab ", "wpa-psk"))
    flags = {n["ssid"]: n["saved"] for n in network.scan()["networks"]}
    assert flags["Captua Lab"] is True and flags[" Captua Lab "] is True
    assert flags["eduroam"] is True and flags["eduroam "] is False


@pytest.mark.unit
def test_an_ssid_is_split_only_at_line_ends(nm):
    from app.core import network
    nm.on(WIFI_LIST_SCAN, stdout=" :Line Sep:40:WPA2\n :Tab\tNet:30:\n")
    ssids = [n["ssid"] for n in network.scan()["networks"]]
    assert ssids == ["Line Sep", "Tab\tNet"]


# Server names, SSIDs and anonymous identities ------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("server_name, ok", [
    ("radius.example.edu", True), ("example.edu", True), ("a-b.example.edu", True), ("radius1.example.edu", True),
    ("radius.example.edu\n", False), ("-radius.example.edu", False), ("radius-.example.edu", False),
    ("radius.-example.edu", False), ("radius.example.edu-", False), ("10.0.0.1", False), ("radius.123", False),
    ("radius.example.edu.", False), ("radius..edu", False),
])
def test_server_name_validation(server_name, ok):
    from app.core.network import enterprise_args, join_problems
    codes = join_problems(ssid="eduroam", security="enterprise", password=PASSWORD, identity=IDENTITY,
                          method="peap", inner_method=None, server_name=server_name)
    assert codes == ([] if ok else ["server_name_invalid"])
    if not ok:
        with pytest.raises(ValueError):
            enterprise_args(method="peap", inner_method=None, identity=IDENTITY, anonymous_identity=None,
                            server_name=server_name)


@pytest.mark.unit
@pytest.mark.parametrize("ssid", ["Captua\nLab", "Captua\rLab", "Captua\x00Lab"])
def test_ssid_with_a_line_break_or_nul_is_invalid(ssid):
    from app.core.network import join_problems
    assert join_problems(ssid=ssid, security="open", password=None, identity=None, method=None,
                         inner_method=None, server_name=None) == ["ssid_invalid"]


@pytest.mark.unit
@pytest.mark.parametrize("anon", ["anon\n@example.edu", "anon\r", "a\x00b"])
def test_anonymous_identity_with_a_line_break_or_nul_is_invalid(anon):
    from app.core.network import join_problems
    assert join_problems(ssid="eduroam", security="enterprise", password=PASSWORD, identity=IDENTITY,
                         method="peap", inner_method=None, server_name="radius.example.edu",
                         anonymous_identity=anon) == ["anonymous_identity_invalid"]


@pytest.mark.unit
def test_enterprise_without_a_method_is_method_required():
    from app.core.network import join_problems
    assert join_problems(ssid="eduroam", security="enterprise", password=PASSWORD, identity=IDENTITY,
                         method=None, inner_method=None, server_name="radius.example.edu") == ["method_required"]


@pytest.mark.unit
def test_runner_turns_a_popen_value_error_into_unavailable(nm):
    from app.core import network
    nm.on("nmcli nul", raises=ValueError(f"embedded null byte {PASSWORD}"))
    with pytest.raises(network.NetworkUnavailable) as exc:
        network._command(["nmcli", "nul"], timeout=5)
    assert PASSWORD not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


@pytest.mark.unit
def test_undecodable_output_is_unavailable(nm):
    from app.core import network
    nm.on(DEVICE_STATUS, undecodable=True)
    with pytest.raises(network.NetworkUnavailable) as exc:
        network.status()
    assert exc.value.__cause__ is None and exc.value.__context__ is None


@pytest.mark.unit
def test_module_docstring_lists_what_needs_a_real_unit():
    from app.core import network
    doc = " ".join(network.__doc__.split())
    still_open = doc[doc.index("Still to be run on a real unit"):]
    for item in ("brcmfmac", "20 s", "trailing tabs"):
        assert item in still_open, item
    for settled in ("802.1X passwords", "several edge spaces", "identity supplied on stdin only",
                    "general permissions", "non-ASCII SSID"):
        assert settled not in still_open, settled
    assert "Secrets were required, but not provided" in doc[:doc.index("Still to be run")]



# ---------------------------------------------------------------------------
# Third review round: the journal window, `\r` in SSIDs
# ---------------------------------------------------------------------------

OLD_PROFILE_LINES = [
    # The old profile for the same SSID, failing, before `connection up` replaced it.
    "wlan0: SME: Trying to authenticate with 02:00:00:00:01:00 (SSID='Captua Lab' freq=2412 MHz)",
    "wlan0: Trying to associate with 02:00:00:00:01:00 (SSID='Captua Lab' freq=2412 MHz)",
    "wlan0: WPA: 4-Way Handshake failed - pre-shared key may be incorrect",
    'wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid="Captua Lab" auth_failures=1 duration=10 reason=WRONG_KEY',
]


def _one_attempt(name="journal-psk-wrong-key.txt"):
    lines = journal_for(name).splitlines()
    return lines[: next(i for i, l in enumerate(lines) if "reason=WRONG_KEY" in l) + 1]


@pytest.mark.unit
def test_the_bench_captures_begin_with_the_local_disconnect():
    for name in ("journal-psk-wrong-key.txt", "journal-eap-wrong-password-stdin.txt"):
        first = fixture(name).splitlines()[0]
        assert "CTRL-EVENT-DISCONNECTED" in first and "locally_generated=1" in first


@pytest.mark.unit
def test_rejoining_the_same_ssid_ignores_the_old_profiles_lines():
    """The old profile's anchor and failure come before the local disconnect
    that `connection up` causes; they do not count toward the new join."""
    from app.core.network import classify
    one = _one_attempt()
    assert classify(OLD_PROFILE_LINES + one, "wlan0", "Captua Lab") is None
    assert classify(OLD_PROFILE_LINES + one, "wlan0", "Captua Lab", final=True)["code"] == "wrong_password"
    # The capture's own DISCONNECTED line is what discards the old lines.
    assert classify(OLD_PROFILE_LINES + one[1:], "wlan0", "Captua Lab")["code"] == "wrong_password"


@pytest.mark.unit
def test_old_profile_lines_alone_give_no_outcome():
    from app.core.network import classify
    disconnect = journal_for("journal-psk-wrong-key.txt").splitlines()[0]
    lines = OLD_PROFILE_LINES + [disconnect] + JOINED_LINES[:2]
    assert classify(lines, "wlan0", "Captua Lab", final=True) is None


@pytest.mark.unit
def test_eap_capture_with_old_lines_before_it_classifies_from_the_new_attempt():
    from app.core.network import classify
    old = [l.replace("Captua Lab", "eduroam") for l in OLD_PROFILE_LINES]
    lines = journal_for("journal-eap-wrong-password-stdin.txt", ssid="eduroam").splitlines()
    assert classify(old + lines, "wlan0", "eduroam")["code"] == "wrong_credentials"
    before_691 = lines[: next(i for i, l in enumerate(lines) if "error 691" in l)]
    assert classify(old + before_691, "wlan0", "eduroam", final=True) is None


def _unix(lines, start, step=0.1):
    """Lines in `journalctl -o short-unix` form, `step` seconds apart from `start`."""
    return [f"{start + i * step:.6f} captua wpa_supplicant[855]: {l}" for i, l in enumerate(lines)]


@pytest.mark.unit
def test_lines_stamped_before_the_start_are_skipped():
    from app.core.network import classify
    lines = journal_for("journal-psk-wrong-key.txt").splitlines()[1:]   # without the local disconnect
    second = [i for i, l in enumerate(lines) if "4-Way Handshake failed" in l][1]
    stamped = _unix(lines[: second + 1], 1794419003.0)
    assert classify(stamped, "wlan0", "Captua Lab")["code"] == "wrong_password"
    # A start after the first attempt's lines leaves only one failure: not decisive.
    first_fail = [i for i, l in enumerate(lines) if "4-Way Handshake failed" in l][0]
    start = 1794419003.0 + (first_fail + 1) * 0.1 - 0.05
    anchor_again = next(i for i in range(first_fail, len(lines)) if "Trying to authenticate" in lines[i])
    assert anchor_again > first_fail
    assert classify(stamped, "wlan0", "Captua Lab", since=start) is None
    assert classify(stamped, "wlan0", "Captua Lab", since=start, final=True)["code"] == "wrong_password"


@pytest.mark.unit
def test_since_is_taken_just_before_connection_up(nm, monkeypatch):
    from app.core import network
    monkeypatch.setattr(network, "_wall_clock", lambda: 1794419000.7 + len(nm.calls))
    _join_nm(nm, up_rc=4, up_waits=1)
    _join()
    order = [" ".join(c) for c in nm.calls]
    up_at = next(i for i, c in enumerate(order) if c.startswith(UP_NEW))
    assert up_at > next(i for i, c in enumerate(order) if c.startswith(ADD))
    journal = nm.ran(JOURNAL)[0]
    assert journal[journal.index("--since") + 1] == f"@{1794419000 + up_at}"


@pytest.mark.unit
def test_an_ssid_may_end_with_a_carriage_return(nm):
    from app.core import network
    nm.on(WIFI_LIST_SCAN, stdout=" :Net\r:40:WPA2\n :Other:30:\n")
    assert [n["ssid"] for n in network.scan()["networks"]] == ["Net\r", "Other"]
    assert network.split_terse(network._lines("a:b\r\n")[0]) == ["a", "b\r"]


@pytest.mark.unit
def test_module_docstring_names_the_built_in_radio():
    from app.core import network
    assert "brcmfmac" in network.__doc__



# ---------------------------------------------------------------------------
# Fourth round: the unit's own output
# ---------------------------------------------------------------------------

# Multiline terse output (`connection show uuid`, `device show`) is not
# escaped: the unit printed `802-11-wireless.ssid:cap:colon`, `... cap edge `,
# `...cap"dq`, `...cap'sq`, `...-test`, and `...cap\bs` raw. Tabular output
# (`connection show`, `device wifi list`) escapes: `cap\\bs:<uuid>...` and
# `cap\:colon` in the unit's listings.
SSID_FORMS = [  # (ssid, tabular field)
    ("cap\\bs", "cap\\\\bs"),
    ("cap:colon", "cap\\:colon"),
    (" cap edge ", " cap edge "),
    ('cap"dq', 'cap"dq'),
    ("cap'sq", "cap'sq"),
    ("-test", "-test"),
    ("cáp-ñ-\U0001F600", "cáp-ñ-\U0001F600"),
]
FORM_UUIDS = [f"0000000{i}-1111-4222-8333-444444444444" for i in range(len(SSID_FORMS))]


@pytest.mark.unit
@pytest.mark.parametrize("ssid, tabular", SSID_FORMS)
def test_multiline_values_are_raw_and_tabular_values_unescaped(ssid, tabular):
    from app.core.network import parse_fields, split_terse
    assert parse_fields(f"802-11-wireless.ssid:{ssid}\n")["802-11-wireless.ssid"] == [ssid]
    assert split_terse(f"{tabular}:{FORM_UUIDS[0]}:802-11-wireless:no", maxsplit=3)[0] == ssid


def _forms_nm(nm):
    listing = "".join(f"{tab}:{u}:802-11-wireless:no\n" for (_, tab), u in zip(SSID_FORMS, FORM_UUIDS))
    nm.on(CONNECTIONS, stdout=fixture("nmcli-connection-show.txt") + listing)
    for (ssid, _), u in zip(SSID_FORMS, FORM_UUIDS):
        nm.on(f"{DETAILS} {u}", stdout=f"802-11-wireless.ssid:{ssid}\n802-11-wireless-security.key-mgmt:wpa-psk\n")


@pytest.mark.unit
def test_saved_networks_read_ssids_with_backslashes_colons_spaces_and_quotes(nm):
    from app.core import network
    _forms_nm(nm)
    saved = {s["uuid"]: (s["name"], s["ssid"]) for s in network.saved_networks()}
    for (ssid, _), u in zip(SSID_FORMS, FORM_UUIDS):
        assert saved[u] == (ssid, ssid)


@pytest.mark.unit
def test_scan_flags_backslash_ssids_as_saved(nm):
    from app.core import network
    _forms_nm(nm)
    rows = "".join(f" :{tab}:50:WPA2\n" for _, tab in SSID_FORMS)
    nm.on(WIFI_LIST_SCAN, stdout=rows)
    flags = {n["ssid"]: n["saved"] for n in network.scan()["networks"]}
    assert flags == {ssid: True for ssid, _ in SSID_FORMS}


@pytest.mark.unit
def test_forget_returns_the_backslash_ssid(nm):
    from app.core import network
    _forms_nm(nm)
    nm.on(DELETE, rc=0)
    assert network.forget(FORM_UUIDS[0], force=False) == "cap\\bs"


@pytest.mark.unit
def test_join_removes_older_profiles_of_a_backslash_ssid(nm):
    _join_nm(nm)
    _forms_nm(nm)
    nm.on(CONNECTIONS, stdout=f"cap\\\\bs:{FORM_UUIDS[0]}:802-11-wireless:no\ncap\\\\bs:{NEW_UUID}:802-11-wireless:yes\n"
                              f"cap\\\\bsx:{FORM_UUIDS[1]}:802-11-wireless:no\n")
    nm.on(f"{DETAILS} {NEW_UUID}", stdout="802-11-wireless.ssid:cap\\bs\n802-11-wireless-security.key-mgmt:wpa-psk\n")
    nm.on(f"{DETAILS} {FORM_UUIDS[1]}", stdout="802-11-wireless.ssid:cap\\bsx\n802-11-wireless-security.key-mgmt:wpa-psk\n")
    assert _join(ssid="cap\\bs").joined
    assert [c[-1] for c in nm.ran(DELETE)] == [FORM_UUIDS[0]]


@pytest.mark.unit
def test_device_show_values_are_raw(nm):
    from app.core import network
    nm.on("nmcli -t -f GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.DNS device show eth0", stdout="")
    nm.on(SHOW_WLAN0, stdout=fixture("nmcli-device-show-wlan0.txt").replace("DC:A6", "DC\\A6"))
    body = network.status()
    wlan0 = body["interfaces"][0]
    assert wlan0["mac"] == "DC\\A6:32:01:02:03" and wlan0["ipv6"] == ["fe80::dea6:32ff:fe01:203/64"]


# A profile that vanishes between the listing and its detail read ------------

VANISHED = "Error: {u} - no such connection profile."   # rc 10, as the unit answered


@pytest.mark.unit
def test_a_profile_deleted_after_the_listing_is_skipped(nm):
    from app.core import network
    nm.on(f"{DETAILS} {EDUROAM_UUID}", rc=10, stderr=VANISHED.format(u=EDUROAM_UUID))
    nm.on(f"nmcli -t -f 802-11-wireless.ssid connection show uuid {EDUROAM_UUID}", rc=10,
          stderr=VANISHED.format(u=EDUROAM_UUID))
    assert [s["uuid"] for s in network.saved_networks()] == [OLD_UUID]
    assert network.scan()["saved"][0]["uuid"] == OLD_UUID


@pytest.mark.unit
def test_a_profile_vanishing_at_the_fallback_read_is_skipped(nm):
    from app.core import network
    nm.on(f"{DETAILS} {EDUROAM_UUID}", rc=10, stderr="Error: invalid field '802-11-wireless-security'")
    nm.on(f"nmcli -t -f 802-11-wireless.ssid connection show uuid {EDUROAM_UUID}", rc=10,
          stderr=VANISHED.format(u=EDUROAM_UUID))
    assert [s["uuid"] for s in network.saved_networks()] == [OLD_UUID]


@pytest.mark.unit
@pytest.mark.parametrize("rc, stderr", [(10, "Error: invalid field '802-11-wireless-security'"),
                                        (8, VANISHED.format(u=EDUROAM_UUID)),
                                        (10, VANISHED.format(u=OLD_UUID))])
def test_other_detail_failures_still_raise(nm, rc, stderr):
    from app.core import network
    nm.on(f"{DETAILS} {EDUROAM_UUID}", rc=rc, stderr=stderr)
    nm.on(f"nmcli -t -f 802-11-wireless.ssid connection show uuid {EDUROAM_UUID}", rc=rc, stderr=stderr)
    with pytest.raises(network.NetworkUnavailable):
        network.saved_networks()


# A wrong SAE password ---------------------------------------------------------

@pytest.mark.unit
def test_classify_wrong_sae_password_from_the_unit():
    from app.core.network import classify
    lines = fixture("journal-sae-wrong-password.txt").splitlines()
    rejects = [i for i, l in enumerate(lines) if "CTRL-EVENT-AUTH-REJECT" in l]
    assert len(rejects) == 3 and not any("WRONG_KEY" in l or "4-Way" in l for l in lines)
    assert classify(lines, "wlan9", "cap-sae") == {"code": "wrong_password", "detail": None, "server": None}
    assert classify(lines[: rejects[0] + 1], "wlan9", "cap-sae") is None
    assert classify(lines[: rejects[0] + 1], "wlan9", "cap-sae", final=True)["code"] == "wrong_password"
    assert classify(lines[: rejects[1] + 1], "wlan9", "cap-sae")["code"] == "wrong_password"
    assert classify(lines, "wlan9", "cap-other", final=True) is None


@pytest.mark.unit
def test_an_auth_reject_that_is_not_sae_is_not_counted():
    from app.core.network import classify
    lines = [l.replace("auth_type=3", "auth_type=0") for l in fixture("journal-sae-wrong-password.txt").splitlines()]
    assert classify(lines, "wlan9", "cap-sae", final=True) is None


@pytest.mark.unit
def test_join_wrong_sae_password_ending_with_nmcli_is_wrong_password(nm):
    """On the unit nmcli ended with exit 4 ("The Wi-Fi network could not be found")."""
    lines = fixture("journal-sae-wrong-password.txt").replace("wlan9: ", "wlan0: ").replace("cap-sae", "NewHome")
    first = lines.splitlines()[:3]
    _join_nm(nm, up_rc=4, up_waits=0, up_stderr="Error: Connection activation failed: The Wi-Fi network could not be found")
    nm.on(JOURNAL, stdout="\n".join(first) + "\n")
    outcome = _join(ssid="NewHome")
    assert nm.ran(ADD)[0][13:] == ["wifi-sec.key-mgmt", "sae"]
    assert outcome.problem == {"code": "wrong_password", "detail": None, "server": None}


@pytest.mark.unit
def test_join_wrong_sae_password_is_stopped_on_the_second_reject(nm):
    lines = fixture("journal-sae-wrong-password.txt").replace("wlan9: ", "wlan0: ").replace("cap-sae", "NewHome")
    _join_nm(nm, up_rc=4, up_waits=None)
    nm.on(JOURNAL, stdout=lines)
    outcome = _join(ssid="NewHome")
    assert outcome.problem["code"] == "wrong_password" and nm.child(UP_NEW)[0].terminated


# passwd-file encoding ----------------------------------------------------------

def _decode_passwd_value(value):
    """The unit's reading of a passwd-file value: `\\x` is `x`."""
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _stdin_values(text):
    return {line.split(":", 1)[0]: _decode_passwd_value(line.split(":", 1)[1]) for line in text.splitlines()}


@pytest.mark.unit
@pytest.mark.parametrize("password, encoded", [
    (" Lead-space-pw", "\\ Lead-space-pw"),
    ("Trail-space-pw ", "Trail-space-pw\\ "),
    ("Back\\slash-pw", "Back\\\\slash-pw"),
    ("\tTab-pw\t", "\\\tTab-pw\\\t"),
    ("  two lead", "\\ \\ two lead"),
    ("in side:colon pw", "in side:colon pw"),
    ("ends\\", "ends\\\\"),
    ("        ", "\\ " * 8),
])
def test_secrets_text_encodes_edge_whitespace_and_backslashes(password, encoded):
    from app.core.network import _secrets_text
    text = _secrets_text(security="password", password=password, identity=None)
    assert text == f"802-11-wireless-security.psk:{encoded}\n"
    assert _stdin_values(text) == {"802-11-wireless-security.psk": password}
    text = _secrets_text(security="enterprise", password=password, identity=f" {password}\\id ")
    assert _stdin_values(text) == {"802-1x.identity": f" {password}\\id ", "802-1x.password": password}


EDGE_JOINS = [
    dict(ssid="Captua Lab", security="password", password=" Lead\\pw-123 "),
    dict(ssid="eduroam", security="enterprise", identity=" -dash\\id@example.edu ", password="\tpw\\x ",
         method="peap", server_name="radius.example.edu"),
]


@pytest.mark.unit
@pytest.mark.parametrize("join", EDGE_JOINS)
@pytest.mark.parametrize("path", list(PATHS))
def test_stdin_decodes_to_the_values_and_argv_holds_no_password(nm, join, path):
    _walk(nm, {**join}, path, None)
    for argv in nm.calls:
        assert all(join["password"] not in arg for arg in argv)
        assert all(join["password"].strip() not in arg for arg in argv)
    for up in nm.child(UP_NEW):
        values = _stdin_values(up.input)
        if join["security"] == "password":
            assert values == {"802-11-wireless-security.psk": join["password"]}
        else:
            assert values == {"802-1x.identity": join["identity"], "802-1x.password": join["password"]}



# ---------------------------------------------------------------------------
# Final round: control characters in passphrases, `restored`, night captures
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("password", ["\tTab-lead-pass", "Tab-trail-pass\t", "in\x01side-pass", "del\x7fete-pass",
                                      "esc\x1bape-pass", "x\x1f-unit-sep"])
def test_a_passphrase_with_a_control_character_is_invalid(password):
    from app.core.network import join_problems
    assert join_problems(ssid="Captua Lab", security="password", password=password, identity=None,
                         method=None, inner_method=None, server_name=None) == ["password_invalid"]


@pytest.mark.unit
@pytest.mark.parametrize("password", ["contraseña-éè", "space inside ok", "cáp-\U0001F600-key", "~!@#$%^&*()"])
def test_a_passphrase_with_non_ascii_or_printable_characters_is_allowed(password):
    from app.core.network import join_problems
    assert join_problems(ssid="Captua Lab", security="password", password=password, identity=None,
                         method=None, inner_method=None, server_name=None) == []


@pytest.mark.unit
@pytest.mark.parametrize("password", ["\tTab-lead-pw", "tab\tinside", "trail\t", "del\x7fete"])
def test_an_8021x_password_may_hold_tabs_and_control_characters(password):
    from app.core.network import join_problems
    assert join_problems(ssid="eduroam", security="enterprise", password=password, identity=IDENTITY,
                         method="peap", inner_method=None, server_name="radius.example.edu") == []


@pytest.mark.unit
def test_restored_docstrings_say_active_or_being_activated_again():
    from app.api import network as routes
    from app.core import network
    assert "being activated again" in " ".join(network._restore.__doc__.split())
    assert "being activated again" in " ".join(routes.get_join.__doc__.split())


@pytest.mark.unit
def test_backslash_ssid_from_the_units_listing_and_detail_read(nm):
    from app.core import network
    uuid_ = "b3ac75ab-ab8f-4107-b240-d4ac75ad3def"
    listing = fixture("nmcli-connection-show-backslash.txt")
    detail = fixture("nmcli-connection-show-uuid-backslash.txt")
    assert listing.startswith("cap\\\\bs:") and detail == "802-11-wireless.ssid:cap\\bs\n"
    nm.on(CONNECTIONS, stdout=listing)
    nm.on(f"{DETAILS} {uuid_}", stdout=detail)
    assert network.saved_networks() == [
        {"uuid": uuid_, "name": "cap\\bs", "ssid": "cap\\bs", "security": "open", "active": True}]


@pytest.mark.unit
def test_sae_single_reject_from_the_unit():
    """The unit's capture: the local disconnect, about 20 s, one SAE reject;
    nmcli then ended with exit 4 and the module reported wrong_password."""
    from app.core.network import classify
    lines = fixture("journal-sae-wrong-password-one-reject.txt").splitlines()
    assert "locally_generated=1" in lines[0] and sum("AUTH-REJECT" in l for l in lines) == 1
    assert classify(lines, "wlan9", "cap-sae") is None
    assert classify(lines, "wlan9", "cap-sae", final=True) == {"code": "wrong_password", "detail": None,
                                                              "server": None}


@pytest.mark.unit
def test_join_from_the_units_single_reject_capture(nm):
    text = fixture("journal-sae-wrong-password-one-reject.txt").replace("wlan9: ", "wlan0: ").replace("cap-sae", "NewHome")
    _join_nm(nm, up_rc=4, up_waits=2, up_stderr="Error: Connection activation failed: The Wi-Fi network could not be found")
    nm.on(JOURNAL, stdout=text)
    outcome = _join(ssid="NewHome")
    assert not nm.child(UP_NEW)[0].terminated
    assert outcome.problem["code"] == "wrong_password"
