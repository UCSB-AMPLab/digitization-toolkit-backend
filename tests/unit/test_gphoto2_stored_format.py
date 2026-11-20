"""The capture format of a gphoto2 DSLR, stored on the unit for each camera body.

The format chosen for a Canon body is kept in the camera registry under the
body's hardware id (the registry entry's "image_format", as on CHDK). The
settings route sets it on the camera and stores it, and answers 200 only when
both hold; a session that opens sets it on the camera unless the camera
already holds it; a record capture applies it under the camera lock, for the
body the open session holds; a session writes the format only when it differs
from the one it last set; and a capture that produces the other file type
clears that memory, so the next capture writes the format again. A unit that
has nothing stored for a body adopts the format the camera holds at open, when
that is a value Captua itself sets.

A real _PTPSession runs against a fake libgphoto2. Each body is a dict of
PTP widgets on a USB port; every commit of a configuration tree
(Camera.set_config) is recorded as one PTP configuration write, with the
widgets it changed. The shutter is not fired: _PTPSession.capture is replaced
by a stub that writes a `.cr2` when the body's imageformat widget is "RAW" or
"RAW + L" and a `.jpg` otherwise, so a test sees the file type produced.

Version: 0.1.0-beta
"""

import logging
import types
from pathlib import Path

import pytest

import capture.backends.gphoto2_backend as gb
from capture.camera_registry import CameraRegistry


ABILITIES = "Canon EOS 1500D"
PORT_A = "usb:001,004"
PORT_B = "usb:001,005"
PORT_C = "usb:001,014"
SERIAL_A = "1111111"
SERIAL_B = "2222222"
SERIAL_C = "3333333"
HW_A = f"canoneos1500d_{SERIAL_A}"
HW_B = f"canoneos1500d_{SERIAL_B}"
HW_C = f"canoneos1500d_{SERIAL_C}"

LOGGER = "test-gphoto2-stored-format"


class _GPhoto2Error(Exception):
    pass


class _Rig:
    """The bodies on the bus, what autodetect reports, and every write made."""

    def __init__(self):
        self.detected = []
        self.bodies = {}
        # (port, {widget: value}) for each committed configuration tree
        self.writes = []
        # (port, widget) -> values the body refuses: set_config raises
        self.refused = {}
        # (port, widget, value): set_config succeeds and the body keeps its value
        self.ignored = set()
        # (port, widget): every read of the widget raises
        self.unreadable = set()
        # (port, widget): reads raise once a write of the widget has been made
        self.unreadable_after_write = set()

    def add(self, port, serial, imageformat="L"):
        widgets = {
            "capturetarget": "Internal RAM",
            "reviewtime": "None",
            "autopoweroff": "0",
            "flashmode": "Off",
            "focusmode": "Manual",
            "cameramodel": "Canon EOS Rebel T7",
            "shutterspeed": "1/60",
            "iso": "100",
            "aperture": "8",
        }
        if serial is not None:
            widgets["serialnumber"] = f"  {serial}  "
        if imageformat is not None:
            widgets["imageformat"] = imageformat
        self.bodies[port] = widgets
        self.detected.append((ABILITIES, port))
        return widgets

    def formats_written(self, port):
        return [
            change["imageformat"] for p, change in self.writes
            if "imageformat" in change and p == port
        ]

    def write_count(self, port):
        return sum(1 for p, _change in self.writes if p == port)

    def produces_raw(self, port):
        return self.bodies[port].get("imageformat") in ("RAW", "RAW + L")


def _fake_gp(rig):
    class Widget:
        def __init__(self, config, name):
            self._config = config
            self._name = name

        def get_value(self):
            port = self._config.port
            written = any(
                p == port and self._name in change for p, change in rig.writes
            )
            if (port, self._name) in rig.unreadable or (
                written and (port, self._name) in rig.unreadable_after_write
            ):
                raise _GPhoto2Error(f"-1: {self._name} could not be read")
            if self._name in self._config.pending:
                return self._config.pending[self._name]
            return rig.bodies[port][self._name]

        def set_value(self, value):
            self._config.pending[self._name] = value

    class Config:
        def __init__(self, port):
            self.port = port
            self.pending = {}

        def get_child_by_name(self, name):
            if name not in rig.bodies[self.port]:
                raise _GPhoto2Error(f"-2: no widget {name}")
            return Widget(self, name)

    class Camera:
        def __init__(self):
            self.port = None

        @staticmethod
        def autodetect():
            return list(rig.detected)

        def set_abilities(self, abilities):
            pass

        def set_port_info(self, info):
            self.port = info

        def init(self):
            pass

        def exit(self):
            pass

        def get_config(self):
            return Config(self.port)

        def set_config(self, cfg):
            for name, value in cfg.pending.items():
                if value in rig.refused.get((self.port, name), ()):
                    raise _GPhoto2Error(f"-1: {name}={value!r} refused")
            applied = {
                name: value for name, value in cfg.pending.items()
                if (self.port, name, value) not in rig.ignored
            }
            rig.bodies[self.port].update(applied)
            rig.writes.append((self.port, dict(cfg.pending)))

        def capture_preview(self):
            return types.SimpleNamespace(get_data_and_size=lambda: b"frame")

    class AbilitiesList:
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
            return path

        def __getitem__(self, path):
            return path

    return types.SimpleNamespace(
        GPhoto2Error=_GPhoto2Error,
        Camera=Camera,
        CameraAbilitiesList=AbilitiesList,
        PortInfoList=PortInfoList,
    )


@pytest.fixture
def rig(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.core.paths import preview_sidecar_of

    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(settings, "PROJECTS_ROOT", str(root))
    rig = _Rig()

    def stub_capture(self, outpath, retry=None, retry_delay=None, focus_check=False):
        outpath.parent.mkdir(parents=True, exist_ok=True)
        if rig.produces_raw(self.port):
            actual = outpath.with_suffix(".cr2")
            actual.write_bytes(b"cr2")
            preview_sidecar_of(actual).write_bytes(b"jpeg")
        else:
            actual = outpath
            actual.write_bytes(b"jpeg")
        return 0.0, actual

    monkeypatch.setattr(gb, "gp", _fake_gp(rig))
    monkeypatch.setattr(gb, "_GP_AVAILABLE", True)
    monkeypatch.setattr(gb._PTPSession, "capture", stub_capture)
    return rig


@pytest.fixture
def registry(tmp_path):
    return CameraRegistry(registry_path=tmp_path / "cameras.json")


@pytest.fixture
def system_log():
    return []


@pytest.fixture
def backend(rig, registry, system_log):
    backend = gb.GPhoto2Backend(logging.getLogger(LOGGER))
    backend._registry_factory = lambda: CameraRegistry(
        registry_path=registry.registry_path
    )
    backend._system_log = lambda **row: system_log.append(row)
    yield backend
    backend.cleanup()


def _store(registry, hardware_id, image_format, index=0):
    registry.register_resolved(hardware_id, {"model": ABILITIES}, index)
    registry.update_image_format(hardware_id, image_format)


def _stored(hardware_id, registry_path):
    entry = CameraRegistry(registry_path=registry_path).get_camera_by_id(hardware_id)
    return (entry or {}).get("image_format")


def _record_config(monkeypatch, backend, index):
    """The configuration a record capture builds, through the route's helper."""
    import capture.service as capture_service
    from app.api.cameras import _put_stored_image_format

    monkeypatch.setattr(capture_service, "_backend", backend)
    config = {"camera_index": index}
    _put_stored_image_format(config, index)
    return types.SimpleNamespace(**config)


def _capture(backend, tmp_path, index, image_format=None, **kwargs):
    config = types.SimpleNamespace(camera_index=index)
    if image_format is not None:
        config.image_format = image_format
    path, _meta = backend.capture_image(tmp_path / f"cam{index}.jpg", config, **kwargs)
    return Path(path)


def _record_capture(monkeypatch, backend, tmp_path, index, config=None):
    if config is None:
        config = _record_config(monkeypatch, backend, index)
    path, _meta = backend.capture_image(tmp_path / f"cam{index}.jpg", config)
    return Path(path)


class _Registry:
    """The real registry, with chosen methods made to fail."""

    def __init__(self, path, fail):
        self._real = CameraRegistry(registry_path=path)
        self._fail = fail

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name in self._fail:
            def failing(*args, **kwargs):
                raise OSError(f"cameras.json: {name} failed")
            return failing
        return attr


def _registry_failing(backend, registry, *names):
    backend._registry_factory = lambda: _Registry(registry.registry_path, set(names))


# --- the settings PUT: 200 only when the camera holds it and it is stored ----


@pytest.mark.unit
def test_a_chosen_format_is_set_on_the_camera_and_stored_for_the_body(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")

    updated = backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    assert rig.bodies[PORT_A]["imageformat"] == "RAW"
    assert updated["image_format"] == "RAW+JPEG"
    assert updated["stored_image_format"] == "RAW+JPEG"
    entry = CameraRegistry(registry_path=registry.registry_path).get_camera_by_id(HW_A)
    assert entry["image_format"] == "RAW+JPEG"
    assert "image_format_updated_at" in entry


@pytest.mark.unit
def test_the_old_name_raw_is_stored_as_raw_plus_jpeg(rig, backend, registry):
    rig.add(PORT_A, SERIAL_A, imageformat="L")

    backend.apply_dslr_settings(0, {"image_format": "RAW"})

    assert _stored(HW_A, registry.registry_path) == "RAW+JPEG"


@pytest.mark.unit
def test_a_body_with_nothing_stored_on_m_stores_the_chosen_format(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="M")

    updated = backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    assert updated["stored_image_format"] == "RAW+JPEG"
    assert _stored(HW_A, registry.registry_path) == "RAW+JPEG"
    assert rig.bodies[PORT_A]["imageformat"] == "RAW"


def _assert_set_back(rig, backend, registry, raised, before, stored):
    assert f"was set back to imageformat={before!r}" in str(raised.value)
    assert rig.bodies[PORT_A]["imageformat"] == before
    assert rig.formats_written(PORT_A)[-1] == before
    assert _stored(HW_A, registry.registry_path) == stored
    assert backend._sessions[0].imageformat_set is None


@pytest.mark.unit
def test_a_format_the_camera_refuses_sets_the_camera_back_and_is_reported(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    backend.get_dslr_settings(0)  # opens and adopts JPEG
    rig.refused[(PORT_A, "imageformat")] = {"RAW"}

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    _assert_set_back(rig, backend, registry, raised, "L", "JPEG")


@pytest.mark.unit
def test_a_write_the_camera_ignores_sets_the_camera_back_and_is_reported(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    backend.get_dslr_settings(0)
    rig.ignored.add((PORT_A, "imageformat", "RAW"))

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    _assert_set_back(rig, backend, registry, raised, "L", "JPEG")
    assert "'L'" in str(raised.value)


@pytest.mark.unit
def test_a_read_back_that_fails_sets_the_camera_back_and_is_reported(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    backend.get_dslr_settings(0)
    rig.unreadable_after_write.add((PORT_A, "imageformat"))

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    assert "set back" in str(raised.value)
    assert rig.formats_written(PORT_A) == ["RAW", "L"]
    assert rig.bodies[PORT_A]["imageformat"] == "L"
    assert _stored(HW_A, registry.registry_path) == "JPEG"


@pytest.mark.unit
def test_a_store_that_fails_sets_the_camera_back_and_is_reported(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    backend.get_dslr_settings(0)  # the session opens and adopts JPEG
    _registry_failing(backend, registry, "update_image_format")

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    assert "not stored" in str(raised.value)
    _assert_set_back(rig, backend, registry, raised, "L", "JPEG")
    assert rig.formats_written(PORT_A) == ["RAW", "L"]


@pytest.mark.unit
def test_a_store_that_fails_with_nothing_stored_sets_the_camera_back_to_m(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="M")
    backend.get_dslr_settings(0)  # "M" is not adopted: nothing is stored
    _registry_failing(backend, registry, "update_image_format")

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    _assert_set_back(rig, backend, registry, raised, "M", None)


@pytest.mark.unit
def test_a_registration_that_fails_sets_the_camera_back_and_is_reported(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="M")
    backend.get_dslr_settings(0)
    _registry_failing(backend, registry, "register_resolved")

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "JPEG"})

    _assert_set_back(rig, backend, registry, raised, "M", None)


@pytest.mark.unit
def test_a_set_back_the_camera_ignores_is_reported_with_what_the_camera_holds(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    backend.get_dslr_settings(0)  # adopts JPEG
    _registry_failing(backend, registry, "update_image_format")
    rig.ignored.add((PORT_A, "imageformat", "L"))

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    message = str(raised.value)
    assert "was set back" not in message
    assert "could not be set back to imageformat='L'" in message
    assert "imageformat='RAW'" in message
    assert rig.bodies[PORT_A]["imageformat"] == "RAW"
    assert backend._sessions[0].imageformat_set is None


@pytest.mark.unit
def test_a_format_that_is_not_taken_leaves_the_other_settings_unwritten(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    backend.get_dslr_settings(0)
    rig.refused[(PORT_A, "imageformat")] = {"RAW"}
    before = rig.write_count(PORT_A)

    with pytest.raises(RuntimeError):
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG", "iso": "400"})

    assert rig.bodies[PORT_A]["iso"] == "100"
    assert all("iso" not in change for _p, change in rig.writes[before:])


@pytest.mark.unit
def test_a_format_that_is_taken_is_followed_by_the_other_settings(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")

    updated = backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG", "iso": "400"})

    assert updated["iso"] == "400"
    assert updated["stored_image_format"] == "RAW+JPEG"
    changes = [c for p, c in rig.writes if p == PORT_A and ("iso" in c or "imageformat" in c)]
    assert changes == [{"imageformat": "RAW"}, {"iso": "400"}]


@pytest.mark.unit
def test_a_format_that_could_not_be_read_before_is_reported_as_not_set_back(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    rig.unreadable.add((PORT_A, "imageformat"))

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})

    assert "could not be set back" in str(raised.value)
    assert _stored(HW_A, registry.registry_path) is None
    assert backend._sessions[0].imageformat_set is None


@pytest.mark.unit
def test_iso_alone_is_set_as_before(rig, backend, registry):
    rig.add(PORT_A, None, imageformat="L")

    updated = backend.apply_dslr_settings(0, {"iso": "400"})

    assert updated["iso"] == "400"
    assert rig.formats_written(PORT_A) == []


@pytest.mark.unit
def test_the_route_answers_502_when_the_format_cannot_be_stored(
    client, monkeypatch, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    api = _installed(client, monkeypatch, backend)
    assert api.get("/cameras/dslr/0/settings").status_code == 200
    _registry_failing(backend, registry, "update_image_format")

    resp = api.put("/cameras/dslr/0/settings", json={"image_format": "RAW+JPEG"})

    assert resp.status_code == 502
    assert "set back" in resp.json()["detail"]
    after = api.get("/cameras/dslr/0/settings").json()
    assert after["image_format"] == after["stored_image_format"] == "JPEG"


@pytest.mark.unit
def test_the_route_answers_502_when_the_camera_refuses_the_format(
    client, monkeypatch, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    rig.refused[(PORT_A, "imageformat")] = {"RAW"}
    api = _installed(client, monkeypatch, backend)

    resp = api.put("/cameras/dslr/0/settings", json={"image_format": "RAW+JPEG"})

    assert resp.status_code == 502
    assert "set back" in resp.json()["detail"]
    assert api.get("/cameras/dslr/0/settings").json()["image_format"] == "JPEG"


# --- a record capture applies the stored format under the camera lock --------


@pytest.mark.unit
def test_the_route_puts_no_format_into_a_dslr_capture(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    backend.get_dslr_settings(0)

    config = _record_config(monkeypatch, backend, 0)

    assert not hasattr(config, "image_format")
    assert backend.capture_image_format(0) == "RAW+JPEG"
    assert _record_capture(monkeypatch, backend, tmp_path, 0, config).suffix == ".cr2"


@pytest.mark.unit
def test_a_capture_built_before_a_put_captures_in_the_new_format(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    backend.get_dslr_settings(0)  # adopts JPEG
    config = _record_config(monkeypatch, backend, 0)

    backend.apply_dslr_settings(0, {"image_format": "RAW+JPEG"})
    produced = _record_capture(monkeypatch, backend, tmp_path, 0, config)

    assert produced.suffix == ".cr2"
    assert rig.formats_written(PORT_A) == ["RAW"]


@pytest.mark.unit
def test_a_capture_built_before_a_rescan_captures_the_new_body_in_its_own_format(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    rig.add(PORT_B, SERIAL_B, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG", index=0)
    _store(registry, HW_B, "RAW+JPEG", index=1)
    _store(registry, HW_C, "JPEG", index=2)
    _record_capture(monkeypatch, backend, tmp_path, 0)
    backend.list_devices()
    # Built while A's session is the one cached at index 0.
    config = _record_config(monkeypatch, backend, 0)

    # Body A is replaced by body C, which a rescan puts on index 0.
    rig.detected = [(ABILITIES, PORT_B)]
    del rig.bodies[PORT_A]
    rig.add(PORT_C, SERIAL_C, imageformat="RAW")
    backend.rescan()
    assert backend._pins == {0: SERIAL_C, 1: SERIAL_B}
    produced = _record_capture(monkeypatch, backend, tmp_path, 0, config)

    assert produced.suffix == ".jpg"
    assert rig.formats_written(PORT_C) == ["L"]
    assert backend.capture_image_format(0) == "JPEG"
    assert _stored(HW_A, registry.registry_path) == "RAW+JPEG"


@pytest.mark.unit
def test_a_session_whose_serial_disagrees_with_the_pin_applies_nothing(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    backend.get_dslr_settings(0)
    rig.bodies[PORT_A]["imageformat"] = "L"
    backend._pins[0] = SERIAL_C  # the pin at the index names another body

    produced = _record_capture(monkeypatch, backend, tmp_path, 0)

    assert backend.capture_image_format(0) is None
    assert rig.formats_written(PORT_A) == ["RAW"]  # the open alone
    assert produced.suffix == ".jpg"


# --- session open ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("held, writes", [("RAW", []), ("L", ["RAW"])])
def test_session_open_writes_the_stored_format_only_when_the_camera_differs(
    rig, backend, registry, held, writes
):
    rig.add(PORT_A, SERIAL_A, imageformat=held)
    _store(registry, HW_A, "RAW+JPEG")

    settings = backend.get_dslr_settings(0)

    assert rig.formats_written(PORT_A) == writes
    assert backend._sessions[0].imageformat_set == "RAW"
    assert settings["image_format"] == "RAW+JPEG"
    assert settings["stored_image_format"] == "RAW+JPEG"


@pytest.mark.unit
def test_a_session_reopened_after_a_close_sets_the_stored_format_again(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    _record_capture(monkeypatch, backend, tmp_path, 0)
    backend._close_session(0)
    # Changed on the body's own menu while no session was open.
    rig.bodies[PORT_A]["imageformat"] = "L"

    produced = _record_capture(monkeypatch, backend, tmp_path, 0)

    assert rig.formats_written(PORT_A) == ["RAW", "RAW"]
    assert produced.suffix == ".cr2"


# --- a serial learnt after the session opened -----------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("path", ["list_devices", "identify_unknown_ports"])
def test_a_serial_learnt_after_open_loads_and_sets_the_stored_format(
    monkeypatch, tmp_path, rig, backend, registry, path
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    rig.unreadable.add((PORT_A, "serialnumber"))
    backend.get_dslr_settings(0)  # opens without a serial
    assert backend._sessions[0].serial == ""
    assert rig.formats_written(PORT_A) == []

    rig.unreadable.discard((PORT_A, "serialnumber"))
    if path == "list_devices":
        backend.list_devices()
    else:
        backend._identify_unknown_ports()

    assert backend._sessions[0].serial == SERIAL_A
    assert rig.formats_written(PORT_A) == ["RAW"]
    assert backend.capture_image_format(0) == "RAW+JPEG"
    assert _record_capture(monkeypatch, backend, tmp_path, 0).suffix == ".cr2"


@pytest.mark.unit
def test_a_serial_learnt_after_open_with_the_format_already_held_writes_nothing(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="RAW")
    _store(registry, HW_A, "RAW+JPEG")
    rig.unreadable.add((PORT_A, "serialnumber"))
    backend.get_dslr_settings(0)
    rig.unreadable.discard((PORT_A, "serialnumber"))

    backend.list_devices()

    assert rig.formats_written(PORT_A) == []
    assert backend._sessions[0].imageformat_set == "RAW"


@pytest.mark.unit
def test_a_serial_learnt_after_open_adopts_the_cameras_format(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="RAW")
    rig.unreadable.add((PORT_A, "serialnumber"))
    backend.get_dslr_settings(0)
    rig.unreadable.discard((PORT_A, "serialnumber"))

    backend.list_devices()

    assert _stored(HW_A, registry.registry_path) == "RAW+JPEG"
    assert rig.formats_written(PORT_A) == []


# --- writes per capture --------------------------------------------------------


@pytest.mark.unit
def test_ptp_writes_per_capture(monkeypatch, tmp_path, rig, backend, registry):
    """Writes per record capture: the flash guard's one, and the format's when it changes."""
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    _record_capture(monkeypatch, backend, tmp_path, 0)  # opens the session

    before = rig.write_count(PORT_A)
    _record_capture(monkeypatch, backend, tmp_path, 0)
    stored_and_set = rig.write_count(PORT_A) - before

    # Changed on the body's menu; the capture that produces a JPEG clears
    # the session's memory, and the next one writes the format.
    rig.bodies[PORT_A]["imageformat"] = "L"
    _record_capture(monkeypatch, backend, tmp_path, 0)
    before = rig.write_count(PORT_A)
    _record_capture(monkeypatch, backend, tmp_path, 0)
    stored_and_different = rig.write_count(PORT_A) - before

    assert (stored_and_set, stored_and_different) == (1, 2)


@pytest.mark.unit
def test_ptp_writes_per_capture_with_nothing_stored(
    monkeypatch, tmp_path, rig, backend, registry, system_log
):
    rig.add(PORT_A, None, imageformat="L")
    _record_capture(monkeypatch, backend, tmp_path, 0)

    before = rig.write_count(PORT_A)
    _record_capture(monkeypatch, backend, tmp_path, 0)

    assert rig.write_count(PORT_A) - before == 1
    assert rig.formats_written(PORT_A) == []
    assert system_log == []


# --- the file a capture produced is checked ------------------------------------


@pytest.mark.unit
def test_a_format_changed_on_the_menu_gives_one_wrong_capture_and_then_a_right_one(
    monkeypatch, tmp_path, caplog, rig, backend, registry, system_log
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    backend.get_dslr_settings(0)
    rig.bodies[PORT_A]["imageformat"] = "L"
    caplog.set_level(logging.WARNING, logger=LOGGER)

    wrong = _record_capture(monkeypatch, backend, tmp_path, 0)
    right = _record_capture(monkeypatch, backend, tmp_path, 0)

    assert wrong.suffix == ".jpg" and wrong.exists()
    assert right.suffix == ".cr2"
    assert rig.formats_written(PORT_A) == ["RAW", "RAW"]
    [row] = system_log
    assert row["level"] == "WARN"
    assert "0" in row["subject"]
    assert "RAW+JPEG" in row["detail"] and "JPEG" in row["detail"]
    assert any("RAW+JPEG" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


@pytest.mark.unit
def test_a_system_log_write_that_raises_leaves_the_capture_as_it_is(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    backend.get_dslr_settings(0)
    rig.bodies[PORT_A]["imageformat"] = "L"

    def failing(**row):
        raise RuntimeError("the system log cannot be written")

    backend._system_log = failing

    produced = _record_capture(monkeypatch, backend, tmp_path, 0)

    assert produced.suffix == ".jpg" and produced.exists()
    assert backend._sessions[0].imageformat_set is None


@pytest.mark.unit
def test_a_format_write_the_camera_ignores_is_noticed_at_each_capture(
    monkeypatch, tmp_path, rig, backend, registry, system_log
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    rig.ignored.add((PORT_A, "imageformat", "RAW"))

    first = _record_capture(monkeypatch, backend, tmp_path, 0)
    second = _record_capture(monkeypatch, backend, tmp_path, 0)

    assert (first.suffix, second.suffix) == (".jpg", ".jpg")
    # The open's write, then the second capture's, after the first cleared
    # the memory.
    assert rig.formats_written(PORT_A) == ["RAW", "RAW"]
    assert len(system_log) == 2


# --- adoption -----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("widget, adopted", [("L", "JPEG"), ("RAW", "RAW+JPEG")])
def test_a_camera_on_a_value_captua_sets_is_adopted_with_no_write(
    monkeypatch, tmp_path, caplog, rig, backend, registry, widget, adopted
):
    rig.add(PORT_A, SERIAL_A, imageformat=widget)
    caplog.set_level(logging.INFO, logger=LOGGER)

    backend.get_dslr_settings(0)

    assert _stored(HW_A, registry.registry_path) == adopted
    assert backend.capture_image_format(0) == adopted
    assert rig.formats_written(PORT_A) == []
    assert any(
        "adopted" in r.getMessage() and adopted in r.getMessage()
        for r in caplog.records
    )
    _record_capture(monkeypatch, backend, tmp_path, 0)
    assert rig.formats_written(PORT_A) == []


@pytest.mark.unit
@pytest.mark.parametrize("widget", ["RAW + L", "M", "Small Fine JPEG"])
def test_a_camera_on_any_other_value_is_not_adopted_and_captures_as_today(
    monkeypatch, tmp_path, caplog, rig, backend, registry, widget, system_log
):
    rig.add(PORT_A, SERIAL_A, imageformat=widget)
    caplog.set_level(logging.WARNING, logger=LOGGER)

    _record_capture(monkeypatch, backend, tmp_path, 0)  # opens the session
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and repr(widget) in r.getMessage()
    ]
    before = rig.write_count(PORT_A)
    config = _record_config(monkeypatch, backend, 0)
    _record_capture(monkeypatch, backend, tmp_path, 0, config)

    assert len(warnings) == 1
    assert _stored(HW_A, registry.registry_path) is None
    assert backend.capture_image_format(0) is None
    assert not hasattr(config, "image_format")
    assert rig.formats_written(PORT_A) == []
    assert rig.bodies[PORT_A]["imageformat"] == widget
    assert rig.write_count(PORT_A) - before == 1
    assert system_log == []


@pytest.mark.unit
def test_a_camera_in_raw_plus_l_shows_raw_plus_jpeg_with_nothing_stored(
    rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="RAW + L")

    settings = backend.get_dslr_settings(0)

    assert settings["image_format"] == "RAW+JPEG"
    assert settings["stored_image_format"] is None


@pytest.mark.unit
def test_adoption_happens_once(monkeypatch, tmp_path, rig, backend, registry):
    rig.add(PORT_A, SERIAL_A, imageformat="RAW")
    backend.get_dslr_settings(0)
    backend._close_session(0)
    rig.bodies[PORT_A]["imageformat"] = "L"

    backend.get_dslr_settings(0)

    assert _stored(HW_A, registry.registry_path) == "RAW+JPEG"
    assert rig.bodies[PORT_A]["imageformat"] == "RAW"


@pytest.mark.unit
def test_adoption_never_overwrites_a_stored_format(rig, backend, registry):
    rig.add(PORT_A, SERIAL_A, imageformat="RAW")
    _store(registry, HW_A, "JPEG")

    backend.get_dslr_settings(0)

    assert _stored(HW_A, registry.registry_path) == "JPEG"
    assert rig.bodies[PORT_A]["imageformat"] == "L"


@pytest.mark.unit
def test_adoption_uses_a_format_stored_between_the_read_and_the_write(
    rig, backend, registry
):
    """The entry is read again inside the registry's write lock before adopting."""
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    calls = []

    def factory():
        opened = CameraRegistry(registry_path=registry.registry_path)
        if not calls:
            # Another writer stores RAW+JPEG after this instance has read the file.
            _store(CameraRegistry(registry_path=registry.registry_path), HW_A, "RAW+JPEG")
        calls.append(opened)
        return opened

    backend._registry_factory = factory
    backend.get_dslr_settings(0)

    assert _stored(HW_A, registry.registry_path) == "RAW+JPEG"
    assert rig.bodies[PORT_A]["imageformat"] == "RAW"
    assert backend.capture_image_format(0) == "RAW+JPEG"


@pytest.mark.unit
def test_storage_that_refuses_writes_adopts_nothing_and_caches_nothing(
    monkeypatch, tmp_path, rig, backend
):
    """The real refused path: the registry reads as empty and refuses writes."""
    from app.core.config import settings

    backend._registry_factory = gb._open_camera_registry
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    missing = tmp_path / "mounts" / "usb" / "captua-projects"
    monkeypatch.setattr(
        "app.core.storage_override.get_storage_override", lambda: str(missing)
    )

    backend.get_dslr_settings(0)

    assert backend.capture_image_format(0) is None
    assert backend.get_dslr_settings(0)["stored_image_format"] is None
    assert rig.formats_written(PORT_A) == []
    assert not missing.exists()

    # The override is gone and a format has been chosen in the meantime: the
    # next session open reads the registry again, and neither a cached
    # "nothing stored" nor an adoption of the camera's "L" replaces it.
    monkeypatch.setattr("app.core.storage_override.get_storage_override", lambda: None)
    default = Path(settings.projects_dir) / "cameras.json"
    _store(CameraRegistry(registry_path=default), HW_A, "RAW+JPEG")
    backend._close_session(0)
    backend.get_dslr_settings(0)

    assert backend.capture_image_format(0) == "RAW+JPEG"
    assert rig.bodies[PORT_A]["imageformat"] == "RAW"
    assert _stored(HW_A, default) == "RAW+JPEG"


@pytest.mark.unit
def test_an_unreadable_camera_format_with_nothing_stored_captures_as_before(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat=None)

    _record_capture(monkeypatch, backend, tmp_path, 0)
    _record_capture(monkeypatch, backend, tmp_path, 0)

    assert _stored(HW_A, registry.registry_path) is None
    assert rig.formats_written(PORT_A) == []


# --- registry reads ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("stored", [None, "RAW+JPEG"])
def test_the_registry_is_read_once_per_session_open(
    monkeypatch, tmp_path, rig, backend, registry, stored
):
    rig.add(PORT_A, SERIAL_A, imageformat="M")  # not adopted
    if stored:
        _store(registry, HW_A, stored)
    opened = []
    backend._registry_factory = lambda: opened.append(1) or CameraRegistry(
        registry_path=registry.registry_path
    )

    backend.get_dslr_settings(0)
    for _ in range(3):
        _record_capture(monkeypatch, backend, tmp_path, 0)
        backend.get_dslr_settings(0)
        backend.capture_image_format(0)

    assert len(opened) == 1


@pytest.mark.unit
def test_a_registry_that_cannot_be_opened_is_not_tried_again_until_the_next_open(
    monkeypatch, tmp_path, rig, backend
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    opened = []

    def broken():
        opened.append(1)
        raise OSError("cameras.json is not readable")

    backend._registry_factory = broken
    for _ in range(3):
        _record_capture(monkeypatch, backend, tmp_path, 0)
        backend.get_dslr_settings(0)

    assert len(opened) == 1
    assert backend.capture_image_format(0) is None
    assert rig.formats_written(PORT_A) == []


# --- the stored format beside the camera's ------------------------------------


@pytest.mark.unit
def test_get_reports_what_the_camera_holds_and_what_is_stored(
    client, monkeypatch, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    api = _installed(client, monkeypatch, backend)
    assert api.get("/cameras/dslr/0/settings").status_code == 200
    rig.bodies[PORT_A]["imageformat"] = "L"

    body = api.get("/cameras/dslr/0/settings").json()

    assert body["image_format"] == "JPEG"
    assert body["stored_image_format"] == "RAW+JPEG"
    for field in ("iso", "shutter_speed", "aperture", "focus_mode", "flash_mode"):
        assert field in body


@pytest.mark.unit
def test_a_difference_seen_by_a_read_makes_the_next_capture_set_the_format(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    backend.get_dslr_settings(0)
    rig.bodies[PORT_A]["imageformat"] = "L"
    backend.get_dslr_settings(0)

    _record_capture(monkeypatch, backend, tmp_path, 0)

    assert rig.formats_written(PORT_A) == ["RAW", "RAW"]


@pytest.mark.unit
def test_get_reports_null_when_nothing_is_stored(rig, backend, registry):
    rig.add(PORT_A, None, imageformat="L")

    assert backend.get_dslr_settings(0)["stored_image_format"] is None


# --- an explicit format wins for its capture -----------------------------------


@pytest.mark.unit
def test_an_explicit_format_wins_for_one_capture_and_the_stored_one_returns(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    backend.get_dslr_settings(0)  # session open

    explicit = _capture(backend, tmp_path, 0, image_format="JPEG")
    record = _record_capture(monkeypatch, backend, tmp_path, 0)

    assert rig.formats_written(PORT_A) == ["RAW", "L", "RAW"]
    assert (explicit.suffix, record.suffix) == (".jpg", ".cr2")


# --- test captures, the focus check and the live preview -----------------------


@pytest.mark.unit
@pytest.mark.parametrize("focus_check", [False, True])
def test_test_captures_and_the_focus_check_apply_the_stored_format_like_any_capture(
    monkeypatch, tmp_path, rig, backend, registry, focus_check
):
    import capture.service as capture_service
    from app.core.config import settings

    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    monkeypatch.setattr(settings, "CAMERA_BACKEND", "gphoto2")
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "_backend", backend)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda index: True)
    seen = []
    real = backend.capture_image

    def spy(output_path, camera_config, **kwargs):
        seen.append(camera_config)
        return real(output_path, camera_config, **kwargs)

    monkeypatch.setattr(backend, "capture_image", spy)

    image = capture_service.test_capture_bytes(0, focus_check=focus_check)[0]

    [config] = seen
    # The builders put no format in the configuration; capture_image applies
    # the body's stored format, as for any capture, so the test capture
    # returns the preview of a .cr2.
    assert getattr(config, "image_format", None) is None
    assert image == b"jpeg"
    assert rig.formats_written(PORT_A) == ["RAW"]


@pytest.mark.unit
def test_a_test_capture_whose_file_is_of_the_other_type_is_checked(
    monkeypatch, tmp_path, rig, backend, registry, system_log
):
    import capture.service as capture_service
    from app.core.config import settings

    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    monkeypatch.setattr(settings, "CAMERA_BACKEND", "gphoto2")
    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "_backend", backend)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda index: True)
    backend.get_dslr_settings(0)
    rig.bodies[PORT_A]["imageformat"] = "L"

    capture_service.test_capture_bytes(0, focus_check=True)

    assert len(system_log) == 1
    assert backend._sessions[0].imageformat_set is None


@pytest.mark.unit
def test_the_live_preview_sets_no_format(rig, backend, registry):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    backend.get_dslr_settings(0)

    assert backend.capture_preview(0) == b"frame"
    assert rig.formats_written(PORT_A) == ["RAW"]


# --- identity -------------------------------------------------------------------


@pytest.mark.unit
def test_a_body_with_no_serial_has_the_format_set_on_the_camera_only(
    caplog, rig, backend, registry
):
    rig.add(PORT_A, None, imageformat="RAW")
    caplog.set_level(logging.WARNING, logger=LOGGER)

    updated = backend.apply_dslr_settings(0, {"image_format": "JPEG"})

    assert updated["image_format"] == "JPEG"
    assert updated["stored_image_format"] is None
    assert rig.formats_written(PORT_A) == ["L"]
    assert not registry.registry_path.exists()
    [warning] = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "no identity" in r.getMessage()
    ]
    assert "camera only" in warning.getMessage()


@pytest.mark.unit
@pytest.mark.parametrize("failure", ["ignored", "unreadable", "refused"])
def test_a_body_with_no_serial_whose_camera_does_not_take_the_format_is_set_back(
    rig, backend, registry, failure
):
    rig.add(PORT_A, None, imageformat="RAW")
    backend.get_dslr_settings(0)
    if failure == "ignored":
        rig.ignored.add((PORT_A, "imageformat", "L"))
    elif failure == "unreadable":
        rig.unreadable_after_write.add((PORT_A, "imageformat"))
    else:
        rig.refused[(PORT_A, "imageformat")] = {"L"}

    with pytest.raises(RuntimeError) as raised:
        backend.apply_dslr_settings(0, {"image_format": "JPEG"})

    assert "set back" in str(raised.value)
    assert rig.bodies[PORT_A]["imageformat"] == "RAW"
    assert rig.formats_written(PORT_A)[-1] == "RAW"
    assert backend._sessions[0].imageformat_set is None
    assert not registry.registry_path.exists()


@pytest.mark.unit
def test_a_body_with_no_serial_captures_what_its_camera_holds(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, None, imageformat="RAW")

    config = _record_config(monkeypatch, backend, 0)
    produced = _record_capture(monkeypatch, backend, tmp_path, 0, config)

    assert rig.formats_written(PORT_A) == []
    assert produced.suffix == ".cr2"
    assert backend.capture_image_format(0) is None
    assert not registry.registry_path.exists()


@pytest.mark.unit
def test_the_route_answers_200_with_null_stored_for_a_body_with_no_serial(
    client, monkeypatch, rig, backend, registry
):
    rig.add(PORT_A, None, imageformat="L")
    api = _installed(client, monkeypatch, backend)

    resp = api.put("/cameras/dslr/0/settings", json={"image_format": "RAW+JPEG"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_format"] == "RAW+JPEG"
    assert resp.json()["stored_image_format"] is None
    assert not registry.registry_path.exists()


@pytest.mark.unit
def test_a_serial_two_bodies_answer_stores_and_sets_nothing(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG")
    backend.get_dslr_settings(0)
    rig.add(PORT_B, SERIAL_A, imageformat="L")
    backend._refresh_port_map()

    with pytest.raises(Exception):
        backend.get_dslr_settings(1)

    assert backend.capture_image_format(0) is None
    assert backend.capture_image_format(1) is None
    assert rig.formats_written(PORT_B) == []


@pytest.mark.unit
def test_two_bodies_capture_in_their_own_stored_formats(
    monkeypatch, tmp_path, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    rig.add(PORT_B, SERIAL_B, imageformat="RAW")
    _store(registry, HW_A, "RAW+JPEG", index=0)
    _store(registry, HW_B, "JPEG", index=1)

    for _ in range(2):
        config0 = _record_config(monkeypatch, backend, 0)
        config1 = _record_config(monkeypatch, backend, 1)
        a = Path(backend.capture_image(tmp_path / "a.jpg", config0)[0])
        b = Path(backend.capture_image(tmp_path / "b.jpg", config1)[0])
        assert (a.suffix, b.suffix) == (".cr2", ".jpg")

    assert rig.formats_written(PORT_A) == ["RAW"]
    assert rig.formats_written(PORT_B) == ["L"]


# --- the route ----------------------------------------------------------------


def _installed(client, monkeypatch, backend):
    import capture.service as capture_service
    from app.main import app
    from app.api.auth import get_current_user
    from app.models.user import User

    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service, "_backend", backend)
    app.dependency_overrides[get_current_user] = lambda: User(
        username="op", email="op@example.com", hashed_password="x",
        role="operator", is_active=True,
    )
    return client


@pytest.fixture
def volume(db_session, test_project):
    from app.models.collection import Collection

    v = Collection(name="V", project_id=test_project.id, filename_stem="co-test-fmt")
    db_session.add(v)
    db_session.commit()
    return v


@pytest.mark.unit
def test_a_dual_capture_through_the_route_gives_each_body_its_own_format(
    client, db_session, test_project, volume, monkeypatch, rig, backend, registry
):
    import capture.project_manager as project_manager_module
    import capture.service as capture_service

    rig.add(PORT_A, SERIAL_A, imageformat="L")
    rig.add(PORT_B, SERIAL_B, imageformat="L")
    _store(registry, HW_A, "RAW+JPEG", index=0)
    _store(registry, HW_B, "JPEG", index=1)
    monkeypatch.setattr(capture_service, "is_camera_connected", lambda idx: True)
    monkeypatch.setattr(
        project_manager_module, "default_camera_config_from_registry",
        lambda camera_index, resolution="high", registry=None: (
            {"camera_index": camera_index, "rotate_deg": 0}, "hw"),
    )
    api = _installed(client, monkeypatch, backend)

    resp = api.post("/cameras/capture/dual", json={
        "project_name": test_project.name, "collection_id": volume.id,
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"], body
    suffixes = sorted(Path(p).suffix for p in body["file_paths"])
    assert suffixes == [".cr2", ".jpg"]
    assert rig.formats_written(PORT_A) == ["RAW"]
    assert rig.formats_written(PORT_B) == []


@pytest.mark.unit
def test_the_route_stores_the_format_and_reports_it(
    client, monkeypatch, rig, backend, registry
):
    rig.add(PORT_A, SERIAL_A, imageformat="L")
    api = _installed(client, monkeypatch, backend)

    resp = api.put("/cameras/dslr/0/settings", json={"image_format": "RAW+JPEG"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["image_format"] == "RAW+JPEG"
    assert resp.json()["stored_image_format"] == "RAW+JPEG"
    assert _stored(HW_A, registry.registry_path) == "RAW+JPEG"
