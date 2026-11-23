"""Which kind of camera the unit drives (NEH-252, NEH-348): the order the kind is chosen in, the detection that claims no device, the stored choice, and the routes that report and change it.

The order: the administrator's stored choice; an explicit CAMERA_BACKEND;
what start-up detection finds; the kind the unit last started with; picamera2.
Every probe here is a stub: no test touches a USB bus or libcamera.

    python -m pytest tests/unit/test_camera_kind.py
"""

import asyncio
import json
import logging

import pytest

from app.core import camera_kind
from capture import service as capture_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probes(monkeypatch, libcamera=0, canon=()):
    """Stub the two hardware probes: a libcamera count, and the Canon PTP product ids on the bus."""
    def lib():
        if isinstance(libcamera, Exception):
            raise libcamera
        return libcamera

    def usb():
        if isinstance(canon, Exception):
            raise canon
        return list(canon)

    monkeypatch.setattr(camera_kind, "_libcamera_count", lib)
    monkeypatch.setattr(camera_kind, "_canon_product_ids", usb)


def _explicit(monkeypatch, value):
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", value)


A2500 = 0x3271
OTHER_CANON = 0x32D1


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_setting_is_unset_by_default(monkeypatch):
    from app.core.config import Settings

    monkeypatch.delenv("CAMERA_BACKEND", raising=False)
    assert Settings(_env_file=None).CAMERA_BACKEND is None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_classify_counts_each_kind():
    found = camera_kind.classify(2, [A2500, A2500, OTHER_CANON])
    assert found == {"connected": {"picamera2": 2, "gphoto2": 1, "chdk": 2},
                     "detected": None, "mixed": True}


@pytest.mark.unit
@pytest.mark.parametrize("libcamera, canon, kind", [
    (2, [], "picamera2"),
    (0, [A2500, A2500], "chdk"),
    (0, [OTHER_CANON], "gphoto2"),
])
def test_one_kind_connected_is_detected(libcamera, canon, kind):
    found = camera_kind.classify(libcamera, canon)
    assert found["detected"] == kind
    assert found["mixed"] is False


@pytest.mark.unit
def test_nothing_connected_detects_nothing():
    found = camera_kind.classify(0, [])
    assert found == {"connected": {"picamera2": 0, "gphoto2": 0, "chdk": 0},
                     "detected": None, "mixed": False}


@pytest.mark.unit
def test_a_failing_probe_is_unknown_and_logged(monkeypatch, caplog):
    """An unknown count is never zero: with the Pi count unknown, one Canon kind is not enough to detect."""
    _probes(monkeypatch, libcamera=RuntimeError("libcamera broke"), canon=[A2500])
    with caplog.at_level(logging.WARNING, logger="app.core.camera_kind"):
        found = camera_kind.detect()
    assert found["connected"] == {"picamera2": None, "gphoto2": 0, "chdk": 1}
    assert found["detected"] is None
    assert found["mixed"] is False
    assert "libcamera" in caplog.text


@pytest.mark.unit
def test_both_probes_failing_detects_nothing_and_does_not_raise(monkeypatch):
    _probes(monkeypatch, libcamera=OSError("no"), canon=OSError("no libusb"))
    found = camera_kind.detect()
    assert found["detected"] is None
    assert found["connected"] == {"picamera2": None, "gphoto2": None, "chdk": None}


STILL_IMAGE = 0x06
SCANNER = 0x190E  # CanoScan LiDE 120


class _Dev:
    """A USB device as pyusb shows it: descriptor fields, and its configurations as lists of interface classes."""

    def __init__(self, vendor, product, broken=None, configs=((STILL_IMAGE,),)):
        self._vendor, self._product, self._broken = vendor, product, broken
        self.configs = [list(c) for c in configs]

    @property
    def bNumConfigurations(self):
        return len(self.configs)

    @property
    def idVendor(self):
        if self._broken == "idVendor":
            raise OSError("descriptor unreadable")
        return self._vendor

    @property
    def idProduct(self):
        if self._broken == "idProduct":
            raise OSError("descriptor unreadable")
        return self._product


CANON = 0x04A9


def _bus(monkeypatch, devices):
    import usb.core

    calls = []

    def find(**kwargs):
        calls.append(kwargs)
        return iter(devices)

    monkeypatch.setattr(usb.core, "find", find)
    _descriptors(monkeypatch)
    monkeypatch.setattr(camera_kind, "_canon_product_ids", camera_kind._real_canon_product_ids)
    return calls


def _descriptors(monkeypatch):
    """pyusb's Configuration and Interface, read from a _Dev's lists; `broken="config"` raises as libusb would."""
    import usb.core

    class Configuration:
        def __init__(self, device, configuration=0):
            if device._broken == "config":
                raise usb.core.USBError("LIBUSB_ERROR_IO")
            self.index = configuration
            self.bNumInterfaces = len(device.configs[configuration])

    class Interface:
        def __init__(self, device, interface=0, alternate_setting=0, configuration=0):
            self.bInterfaceClass = device.configs[configuration][interface]

    monkeypatch.setattr(usb.core, "Configuration", Configuration)
    monkeypatch.setattr(usb.core, "Interface", Interface)


@pytest.mark.unit
def test_a_canon_scanner_alone_is_no_camera(monkeypatch):
    _bus(monkeypatch, [_Dev(CANON, SCANNER, configs=((0xFF,),))])
    found = camera_kind.detect()
    assert found["connected"] == {"picamera2": 0, "gphoto2": 0, "chdk": 0}
    assert found["detected"] is None


@pytest.mark.unit
def test_a_canon_scanner_beside_an_a2500_leaves_chdk_detected(monkeypatch):
    _bus(monkeypatch, [_Dev(CANON, SCANNER, configs=((0xFF,),)), _Dev(CANON, A2500)])
    found = camera_kind.detect()
    assert found["connected"] == {"picamera2": 0, "gphoto2": 0, "chdk": 1}
    assert (found["detected"], found["mixed"]) == ("chdk", False)


@pytest.mark.unit
def test_an_eos_with_a_still_image_interface_is_gphoto2(monkeypatch):
    _bus(monkeypatch, [_Dev(CANON, OTHER_CANON, configs=((0xFF, STILL_IMAGE),))])
    assert camera_kind.detect()["detected"] == "gphoto2"


@pytest.mark.unit
def test_an_a2500_counts_on_its_product_id_whatever_its_interfaces(monkeypatch):
    _bus(monkeypatch, [_Dev(CANON, A2500, configs=((0x08,),))])
    assert camera_kind.detect()["connected"]["chdk"] == 1


@pytest.mark.unit
def test_a_configuration_that_cannot_be_read_makes_both_canon_counts_unknown(monkeypatch):
    _bus(monkeypatch, [_Dev(CANON, OTHER_CANON, broken="config"), _Dev(CANON, A2500)])
    found = camera_kind.detect()
    assert found["connected"] == {"picamera2": 0, "gphoto2": None, "chdk": None}
    assert found["detected"] is None


@pytest.mark.unit
def test_a_non_canon_device_is_never_asked_for_its_configuration(monkeypatch):
    _bus(monkeypatch, [_Dev(0x1D6B, 0x0002, broken="config")])
    assert camera_kind.detect()["connected"]["gphoto2"] == 0


@pytest.mark.unit
def test_the_scan_reads_every_device_on_the_bus(monkeypatch):
    calls = _bus(monkeypatch, [_Dev(CANON, A2500), _Dev(CANON, OTHER_CANON), _Dev(0x1D6B, 0x0002)])
    assert camera_kind.detect()["connected"] == {"picamera2": 0, "gphoto2": 1, "chdk": 1}
    # Every device, not usb.core.find's own idVendor filter, which drops a
    # device whose descriptor cannot be read instead of reporting it.
    assert calls == [{"find_all": True}]


@pytest.mark.unit
@pytest.mark.parametrize("broken", ["idVendor", "idProduct"])
def test_an_unreadable_device_beside_an_a2500_makes_both_canon_counts_unknown(monkeypatch, broken):
    _bus(monkeypatch, [_Dev(CANON, OTHER_CANON, broken=broken), _Dev(CANON, A2500)])
    found = camera_kind.detect()
    assert found["connected"] == {"picamera2": 0, "gphoto2": None, "chdk": None}
    assert found["detected"] is None
    monkeypatch.setattr(camera_kind, "_in_effect", {"kind": "gphoto2", "source": "environment"})
    got = camera_kind.report(None)
    assert got["mismatch"] is False and got["detected"] is None


@pytest.mark.unit
def test_start_up_does_not_choose_from_an_incomplete_scan(monkeypatch):
    _explicit(monkeypatch, None)
    _bus(monkeypatch, [_Dev(CANON, OTHER_CANON, broken="idProduct"), _Dev(CANON, A2500)])
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("picamera2", "default")


@pytest.mark.unit
def test_an_enumeration_that_fails_part_way_is_unknown(monkeypatch):
    import usb.core

    def find(**kwargs):
        yield _Dev(CANON, A2500)
        raise usb.core.USBError("enumeration failed")

    monkeypatch.setattr(usb.core, "find", find)
    _descriptors(monkeypatch)
    monkeypatch.setattr(camera_kind, "_canon_product_ids", camera_kind._real_canon_product_ids)
    assert camera_kind.detect()["connected"]["chdk"] is None


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_store_path_defaults_to_the_data_directory(tmp_path):
    from types import SimpleNamespace

    s = SimpleNamespace(CAMERA_KIND_FILE="", data_dir=tmp_path)
    assert camera_kind.store_path_for(s) == tmp_path / "camera-kind.json"
    s = SimpleNamespace(CAMERA_KIND_FILE=str(tmp_path / "x.json"), data_dir=tmp_path)
    assert camera_kind.store_path_for(s) == tmp_path / "x.json"


@pytest.mark.unit
def test_a_missing_store_is_no_choice():
    assert not camera_kind.store_path().exists()
    assert camera_kind.stored_choice() is None
    assert camera_kind.last_used() is None


@pytest.mark.unit
@pytest.mark.parametrize("content", ["{not json", "[]", '{"choice": "nikon"}', '{"choice": 3}'])
def test_an_unreadable_store_is_no_choice_and_logged(content, caplog):
    path = camera_kind.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="app.core.camera_kind"):
        assert camera_kind.stored_choice() is None
    assert caplog.text


@pytest.mark.unit
def test_choice_and_last_used_are_kept_together():
    camera_kind.record_last_used("gphoto2")
    camera_kind.store_choice("chdk")
    assert camera_kind.stored_choice() == "chdk"
    assert camera_kind.last_used() == "gphoto2"
    camera_kind.clear_choice()
    assert camera_kind.stored_choice() is None
    assert camera_kind.last_used() == "gphoto2"
    on_disk = json.loads(camera_kind.store_path().read_text(encoding="utf-8"))
    assert on_disk == {"choice": None, "last_used": "gphoto2"}


@pytest.mark.unit
def test_overlapping_writers_leave_a_whole_store():
    import threading

    errors = []
    barrier = threading.Barrier(16)

    def writer(i):
        try:
            barrier.wait(timeout=5)
            for _ in range(20):
                if i % 2:
                    camera_kind.store_choice("chdk" if i % 4 == 1 else "gphoto2")
                else:
                    camera_kind.record_last_used("picamera2" if i % 4 == 0 else "gphoto2")
        except Exception as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
    for t in threads:
        t.daemon = True
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "a store writer did not finish"
    camera_kind.store_choice("chdk")
    camera_kind.record_last_used("gphoto2")
    assert errors == []
    data = json.loads(camera_kind.store_path().read_text(encoding="utf-8"))
    assert data == {"choice": "chdk", "last_used": "gphoto2"}
    assert [p.name for p in camera_kind.store_path().parent.iterdir()] == ["camera-kind.json"]


@pytest.mark.unit
def test_every_store_mutation_holds_the_store_lock(monkeypatch):
    seen = []

    real = camera_kind._write_store

    def write(data):
        seen.append(camera_kind._store_lock.locked())
        real(data)

    monkeypatch.setattr(camera_kind, "_write_store", write)
    camera_kind.store_choice("chdk")
    camera_kind.record_last_used("gphoto2")
    camera_kind.clear_choice()
    assert seen == [True, True, True]


# ---------------------------------------------------------------------------
# The order of precedence
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_stored_choice_wins_over_the_environment(monkeypatch):
    _explicit(monkeypatch, "picamera2")
    _probes(monkeypatch, libcamera=2)
    camera_kind.store_choice("gphoto2")
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("gphoto2", "stored")


@pytest.mark.unit
def test_an_explicit_value_wins_over_detection(monkeypatch):
    _explicit(monkeypatch, "chdk")
    _probes(monkeypatch, libcamera=2)
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("chdk", "environment")


@pytest.mark.unit
def test_an_explicit_value_is_passed_on_as_written(monkeypatch):
    """The start-up guard reads it and refuses a value the build does not have."""
    _explicit(monkeypatch, "gphoto")
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("gphoto", "environment")


@pytest.mark.unit
@pytest.mark.parametrize("unset", [None, "", "   "])
def test_an_unset_value_is_detected(monkeypatch, unset):
    _explicit(monkeypatch, unset)
    _probes(monkeypatch, canon=[A2500, A2500])
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("chdk", "detected")
    assert r.detection["connected"]["chdk"] == 2


@pytest.mark.unit
def test_nothing_detected_falls_back_to_the_last_kind_used(monkeypatch):
    _explicit(monkeypatch, None)
    _probes(monkeypatch)
    camera_kind.record_last_used("gphoto2")
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("gphoto2", "last_used")


@pytest.mark.unit
def test_nothing_detected_and_nothing_used_is_picamera2(monkeypatch):
    _explicit(monkeypatch, None)
    _probes(monkeypatch)
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("picamera2", "default")


@pytest.mark.unit
def test_mixed_kinds_fall_back_and_report_the_mix(monkeypatch, caplog):
    _explicit(monkeypatch, None)
    _probes(monkeypatch, libcamera=1, canon=[OTHER_CANON])
    camera_kind.record_last_used("chdk")
    with caplog.at_level(logging.WARNING, logger="app.core.camera_kind"):
        r = camera_kind.resolve()
    assert (r.kind, r.source) == ("chdk", "last_used")
    assert r.detection["mixed"] is True and r.detection["detected"] is None
    assert "more than one kind" in caplog.text


@pytest.mark.unit
def test_a_corrupt_store_falls_through_to_the_environment(monkeypatch):
    path = camera_kind.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\x00\x00", encoding="utf-8")
    _explicit(monkeypatch, "gphoto2")
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("gphoto2", "environment")


@pytest.mark.unit
def test_detection_does_not_run_when_a_kind_is_given(monkeypatch):
    _explicit(monkeypatch, "gphoto2")
    _probes(monkeypatch, libcamera=RuntimeError("must not run"), canon=RuntimeError("must not run"))
    called = []
    monkeypatch.setattr(camera_kind, "detect", lambda: called.append(1))
    camera_kind.resolve()
    assert called == []


# ---------------------------------------------------------------------------
# Which drivers this unit has
# ---------------------------------------------------------------------------

def _drivers(monkeypatch, picamera2=True, gphoto2=True, chdk=True, rpicam=True):
    monkeypatch.setattr(camera_kind, "_availability",
                        lambda: {"picamera2": picamera2, "gphoto2": gphoto2, "chdk": chdk})
    monkeypatch.setattr(camera_kind, "_rpicam_available", lambda: rpicam)


@pytest.mark.unit
@pytest.mark.parametrize("canon, kind", [([OTHER_CANON], "gphoto2"), ([A2500, A2500], "chdk")])
def test_no_pi_driver_at_all_lets_start_up_detect_the_canon_kind(monkeypatch, canon, kind):
    _explicit(monkeypatch, None)
    _drivers(monkeypatch, picamera2=False, rpicam=False)
    _probes(monkeypatch, libcamera=camera_kind.ProbeUnavailable("picamera2 cannot be imported"), canon=canon)
    r = camera_kind.resolve()
    assert (r.kind, r.source) == (kind, "detected")


@pytest.mark.unit
def test_rpicam_still_on_the_path_keeps_the_pi_count_unknown(monkeypatch):
    _explicit(monkeypatch, None)
    _drivers(monkeypatch, picamera2=False, rpicam=True)
    _probes(monkeypatch, libcamera=camera_kind.ProbeUnavailable("picamera2 cannot be imported"), canon=[OTHER_CANON])
    camera_kind.record_last_used("chdk")
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("chdk", "last_used")


@pytest.mark.unit
def test_the_report_keeps_an_unknown_pi_count_whatever_the_path_has(monkeypatch):
    _drivers(monkeypatch, picamera2=False, rpicam=False)
    _probes(monkeypatch, libcamera=camera_kind.ProbeUnavailable("x"), canon=[OTHER_CANON])
    monkeypatch.setattr(camera_kind, "_in_effect", {"kind": "gphoto2", "source": "detected"})
    got = camera_kind.report(None)
    assert got["connected"]["picamera2"] is None


@pytest.mark.unit
@pytest.mark.parametrize("missing, canon", [("gphoto2", [OTHER_CANON]), ("chdk", [A2500])])
def test_bodies_of_a_kind_without_its_driver_are_not_chosen(monkeypatch, caplog, missing, canon):
    _explicit(monkeypatch, None)
    _drivers(monkeypatch, **{missing: False})
    _probes(monkeypatch, libcamera=0, canon=canon)
    with caplog.at_level(logging.WARNING, logger="app.core.camera_kind"):
        r = camera_kind.resolve()
    assert (r.kind, r.source) == ("picamera2", "default")
    assert r.detection["detected"] == missing
    assert "driver" in caplog.text and missing in caplog.text


@pytest.mark.unit
def test_the_report_says_which_drivers_are_missing(monkeypatch):
    _drivers(monkeypatch, gphoto2=False)
    _probes(monkeypatch, libcamera=0, canon=[OTHER_CANON, OTHER_CANON])
    monkeypatch.setattr(camera_kind, "_in_effect", {"kind": "picamera2", "source": "default"})
    got = camera_kind.report(None)
    assert got["detected"] == "gphoto2"
    assert got["available"] == {"picamera2": True, "gphoto2": False, "chdk": True}
    assert got["mismatch"] is True


@pytest.mark.unit
def test_availability_reads_the_driver_modules(monkeypatch):
    from capture.backends import chdk_backend, gphoto2_backend, picamera2_backend

    monkeypatch.setattr(picamera2_backend, "_PICAMERA2_AVAILABLE", False)
    monkeypatch.setattr(gphoto2_backend, "_GP_AVAILABLE", True)
    monkeypatch.setattr(chdk_backend, "_PYCHDK_AVAILABLE", False)
    assert camera_kind._real_availability() == {"picamera2": False, "gphoto2": True, "chdk": False}


@pytest.mark.unit
def test_rpicam_is_looked_up_on_the_path_without_running_it(monkeypatch):
    import shutil

    asked = []
    monkeypatch.setattr(shutil, "which", lambda name: asked.append(name) or None)
    assert camera_kind._real_rpicam_available() is False
    assert asked == ["rpicam-still"]


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------

def _stub_lifespan(monkeypatch):
    import app.core.db as core_db
    import app.core.device_id as device_id
    import app.core.storage_ops as storage_ops
    import app.core.unit_mode as unit_mode
    from app import main as app_main

    monkeypatch.setattr(app_main, "init_db", lambda: None)
    monkeypatch.setattr(app_main, "assert_schema_at_head", lambda: None)
    monkeypatch.setattr(core_db, "SessionLocal", lambda: type("S", (), {"close": lambda self: None})())
    monkeypatch.setattr(storage_ops, "reconcile_pending_rename", lambda db: None)
    monkeypatch.setattr(unit_mode, "initialise", lambda: None)
    monkeypatch.setattr(device_id, "initialise", lambda: None)
    monkeypatch.setattr(capture_service, "shutdown_backend", lambda: None)
    scans = []
    monkeypatch.setattr(app_main, "_start_camera_startup_scan", lambda name: scans.append(name))
    return app_main, scans


def _serve(app_main):
    async def serve():
        async with app_main.app.router.lifespan_context(app_main.app):
            pass

    asyncio.run(serve())


@pytest.mark.unit
def test_start_up_writes_the_detected_kind_where_every_reader_reads_it(monkeypatch):
    from app.core import config

    app_main, scans = _stub_lifespan(monkeypatch)
    _explicit(monkeypatch, None)
    monkeypatch.setattr(config.settings, "CAMERA_BACKEND", None)
    _probes(monkeypatch, canon=[OTHER_CANON])
    _serve(app_main)
    assert capture_service.settings.CAMERA_BACKEND == "gphoto2"
    assert config.settings.CAMERA_BACKEND == "gphoto2"
    assert scans == ["gphoto2"]
    # Nothing was built: the start-up scan is stubbed, so nothing is remembered.
    assert camera_kind.last_used() is None
    assert camera_kind.in_effect() == {"kind": "gphoto2", "source": "detected"}


@pytest.mark.unit
def test_start_up_keeps_an_explicit_value(monkeypatch):
    app_main, scans = _stub_lifespan(monkeypatch)
    _explicit(monkeypatch, " CHDK ")
    _probes(monkeypatch, libcamera=RuntimeError("must not run"))
    _serve(app_main)
    assert scans == ["chdk"]
    assert camera_kind.in_effect() == {"kind": "chdk", "source": "environment"}


class _Built:
    def __init__(self, logger):
        pass

    def get_backend_name(self):
        return "built"


class _Fails:
    def __init__(self, logger):
        raise RuntimeError("python-gphoto2 is not installed")


@pytest.mark.unit
def test_a_kind_whose_driver_fails_to_build_is_not_remembered(monkeypatch):
    """The start passes the name-only guard, then the driver cannot be built: the next start must not fall back to it."""
    app_main, scans = _stub_lifespan(monkeypatch)
    _explicit(monkeypatch, "gphoto2")
    monkeypatch.setattr(capture_service, "_backend", None)
    monkeypatch.setitem(capture_service._BACKEND_CONSTRUCTORS, "gphoto2", _Fails)
    _serve(app_main)
    with pytest.raises(RuntimeError):
        capture_service.get_backend()
    assert camera_kind.last_used() is None


@pytest.mark.unit
def test_the_first_driver_built_in_the_process_is_remembered_once(monkeypatch):
    _explicit(monkeypatch, "chdk")
    monkeypatch.setattr(capture_service, "_backend", None)
    monkeypatch.setitem(capture_service._BACKEND_CONSTRUCTORS, "chdk", _Built)
    writes = []
    real = camera_kind.record_last_used
    monkeypatch.setattr(camera_kind, "record_last_used", lambda kind: writes.append(kind) or real(kind))
    capture_service.get_backend()
    capture_service.shutdown_backend()
    capture_service.get_backend()
    capture_service.shutdown_backend()
    assert camera_kind.last_used() == "chdk"
    assert writes == ["chdk"]


@pytest.mark.unit
def test_a_store_that_cannot_be_written_does_not_stop_the_build(monkeypatch):
    _explicit(monkeypatch, "chdk")
    monkeypatch.setattr(capture_service, "_backend", None)
    monkeypatch.setitem(capture_service._BACKEND_CONSTRUCTORS, "chdk", _Built)

    def fail(data):
        raise OSError("read-only file system")

    monkeypatch.setattr(camera_kind, "_write_store", fail)
    assert isinstance(capture_service.get_backend(), _Built)
    capture_service.shutdown_backend()


@pytest.mark.unit
def test_the_last_kind_used_is_skipped_when_its_driver_is_missing(monkeypatch):
    _explicit(monkeypatch, None)
    _drivers(monkeypatch, gphoto2=False)
    _probes(monkeypatch)
    camera_kind.record_last_used("gphoto2")
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("picamera2", "default")


@pytest.mark.unit
def test_the_last_kind_used_is_used_when_its_driver_is_there(monkeypatch):
    _explicit(monkeypatch, None)
    _drivers(monkeypatch, gphoto2=True)
    _probes(monkeypatch)
    camera_kind.record_last_used("gphoto2")
    r = camera_kind.resolve()
    assert (r.kind, r.source) == ("gphoto2", "last_used")


@pytest.mark.unit
def test_start_up_with_probes_failing_still_starts(monkeypatch):
    app_main, scans = _stub_lifespan(monkeypatch)
    _explicit(monkeypatch, None)
    _probes(monkeypatch, libcamera=RuntimeError("x"), canon=RuntimeError("y"))
    _serve(app_main)
    assert scans == ["picamera2"]
    assert camera_kind.in_effect() == {"kind": "picamera2", "source": "default"}


@pytest.mark.unit
def test_start_up_refusing_a_value_remembers_nothing(monkeypatch):
    app_main, scans = _stub_lifespan(monkeypatch)
    _explicit(monkeypatch, "gphoto")
    with pytest.raises(capture_service.UnknownCameraBackend):
        _serve(app_main)
    assert camera_kind.last_used() is None


@pytest.mark.unit
def test_an_unwritable_store_does_not_stop_start_up(monkeypatch, tmp_path):
    app_main, scans = _stub_lifespan(monkeypatch)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(camera_kind, "store_path", lambda: blocker / "camera-kind.json")
    _explicit(monkeypatch, "gphoto2")
    _serve(app_main)
    assert scans == ["gphoto2"]


# ---------------------------------------------------------------------------
# GET /cameras/capabilities
# ---------------------------------------------------------------------------

class _Backend:
    def __init__(self, name, held=None):
        self.name = name
        self.held = held

    def get_capabilities(self):
        return {"live_preview": True}

    def get_backend_name(self):
        return self.name


class _HoldingBackend(_Backend):
    def held_body_count(self):
        return self.held


def _caps(client, monkeypatch, backend, source="environment"):
    from tests.unit.test_capture_manifest_export import _as

    monkeypatch.setattr(capture_service, "get_backend", lambda: backend)
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", backend.get_backend_name())
    monkeypatch.setattr(camera_kind, "_in_effect", {"kind": backend.get_backend_name(), "source": source})
    resp = _as(client, "operator").get("/cameras/capabilities")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.unit
def test_capabilities_report_the_kind_and_what_is_connected(client, monkeypatch):
    _probes(monkeypatch, libcamera=2)
    body = _caps(client, monkeypatch, _Backend("picamera2"), source="detected")
    assert body["backend"] == "picamera2"
    assert body["live_preview"] is True
    assert body["configured"] == "picamera2"
    assert body["source"] == "detected"
    assert body["detected"] == "picamera2"
    assert body["connected"] == {"picamera2": 2, "gphoto2": 0, "chdk": 0}
    assert body["mixed"] is False
    assert body["mismatch"] is False


@pytest.mark.unit
def test_capabilities_report_a_mismatch(client, monkeypatch):
    _probes(monkeypatch, libcamera=0, canon=[A2500, A2500])
    body = _caps(client, monkeypatch, _Backend("picamera2"))
    assert body["detected"] == "chdk"
    assert body["mismatch"] is True


@pytest.mark.unit
def test_nothing_connected_is_no_mismatch(client, monkeypatch):
    _probes(monkeypatch)
    body = _caps(client, monkeypatch, _Backend("gphoto2"))
    assert body["mismatch"] is False
    assert body["detected"] is None


@pytest.mark.unit
def test_the_fresh_scan_wins_over_a_stale_driver_map(client, monkeypatch):
    """An EOS scanned, unplugged, an A2500 attached: the bus shows chdk only, and the gphoto2 driver's map is not used."""
    _probes(monkeypatch, libcamera=0, canon=[A2500])
    body = _caps(client, monkeypatch, _HoldingBackend("gphoto2", held=1))
    assert body["connected"] == {"picamera2": 0, "gphoto2": 0, "chdk": 1}
    assert body["detected"] == "chdk"
    assert body["mixed"] is False
    assert body["mismatch"] is True


@pytest.mark.unit
def test_the_drivers_count_is_used_only_when_the_scan_cannot_run(client, monkeypatch):
    _probes(monkeypatch, libcamera=0, canon=ImportError("no pychdk"))
    body = _caps(client, monkeypatch, _HoldingBackend("chdk", held=2))
    assert body["connected"] == {"picamera2": 0, "gphoto2": None, "chdk": 2}
    assert body["mismatch"] is False


@pytest.mark.unit
def test_capabilities_with_the_probes_failing_still_answer(client, monkeypatch):
    _probes(monkeypatch, libcamera=RuntimeError("x"), canon=RuntimeError("y"))
    body = _caps(client, monkeypatch, _Backend("chdk"))
    assert body["connected"] == {"picamera2": None, "gphoto2": None, "chdk": None}
    assert body["detected"] is None
    assert body["mismatch"] is False


@pytest.mark.unit
def test_capabilities_never_take_a_camera_lock_or_open_a_session(client, monkeypatch):
    """The answer is built from the bus scan and the driver's own count; nothing on the backend but its name, capabilities and count is called."""
    calls = []

    class Strict(_HoldingBackend):
        def __getattr__(self, name):
            calls.append(name)
            raise AttributeError(name)

    _probes(monkeypatch, canon=[A2500])
    _caps(client, monkeypatch, Strict("chdk", held=1))
    assert calls == []


@pytest.mark.unit
def test_held_body_counts_read_the_drivers_maps():
    from capture.backends.chdk_backend import ChdkBackend
    from capture.backends.gphoto2_backend import GPhoto2Backend
    import threading

    chdk = ChdkBackend.__new__(ChdkBackend)
    chdk._map_lock = threading.Lock()
    chdk._bodies = {(1, 2): object(), (1, 3): object()}
    assert chdk.held_body_count() == 2

    gp = GPhoto2Backend.__new__(GPhoto2Backend)
    gp._map_lock = threading.Lock()
    gp._port_map = {0: ("Canon EOS", "usb:001,004")}
    assert gp.held_body_count() == 1


@pytest.mark.unit
@pytest.mark.parametrize("which", ["chdk", "gphoto2"])
def test_a_held_map_lock_never_blocks_the_count(which):
    """With the driver's map lock held (a scan in progress), the count answers at once with the last count published."""
    import threading
    import time
    from capture.backends.chdk_backend import ChdkBackend
    from capture.backends.gphoto2_backend import GPhoto2Backend

    if which == "chdk":
        backend = ChdkBackend.__new__(ChdkBackend)
        backend._bodies = {(1, 2): object()}
    else:
        backend = GPhoto2Backend.__new__(GPhoto2Backend)
        backend._port_map = {0: ("Canon EOS", "usb:001,004")}
    backend._map_lock = threading.Lock()
    assert backend.held_body_count() == 1
    answers = []
    # Asked from another thread, with a bound: a count that waited on the
    # lock would otherwise hang this test instead of failing it.
    asker = threading.Thread(target=lambda: answers.append(backend.held_body_count()), daemon=True)
    assert backend._map_lock.acquire(timeout=5)
    try:
        started = time.monotonic()
        asker.start()
        asker.join(timeout=2)
        ended = not asker.is_alive()
        elapsed = time.monotonic() - started
    finally:
        backend._map_lock.release()
    asker.join(timeout=5)
    assert ended, "held_body_count waited on the map lock"
    assert elapsed < 0.5
    assert answers == [1]


@pytest.mark.unit
def test_a_held_map_lock_before_any_count_is_unknown():
    import threading
    from capture.backends.gphoto2_backend import GPhoto2Backend

    gp = GPhoto2Backend.__new__(GPhoto2Backend)
    gp._map_lock = threading.Lock()
    gp._port_map = {}
    answers = []
    asker = threading.Thread(target=lambda: answers.append(gp.held_body_count()), daemon=True)
    assert gp._map_lock.acquire(timeout=5)
    try:
        asker.start()
        asker.join(timeout=2)
        ended = not asker.is_alive()
    finally:
        gp._map_lock.release()
    asker.join(timeout=5)
    assert ended, "held_body_count waited on the map lock"
    assert answers == [None]


@pytest.mark.unit
def test_the_picamera2_driver_counts_only_what_it_has_listed():
    from capture.backends.picamera2_backend import Picamera2Backend

    pc = Picamera2Backend.__new__(Picamera2Backend)
    pc._camera_info = None
    assert pc.held_body_count() is None
    pc._camera_info = [{"Model": "imx477"}, {"Model": "imx477"}]
    assert pc.held_body_count() == 2


# The truth table of report(): (libcamera probe, Canon probe, driver in
# effect and its own count) -> connected, detected, mixed, mismatch.
# "x" is a probe that cannot run.
TRUTH = [
    # libcamera, canon, driver, held, connected, detected, mixed, mismatch
    (2, [], "picamera2", None, {"picamera2": 2, "gphoto2": 0, "chdk": 0}, "picamera2", False, False),
    (0, [A2500], "picamera2", None, {"picamera2": 0, "gphoto2": 0, "chdk": 1}, "chdk", False, True),
    (0, [], "gphoto2", None, {"picamera2": 0, "gphoto2": 0, "chdk": 0}, None, False, False),
    (1, [OTHER_CANON], "chdk", None, {"picamera2": 1, "gphoto2": 1, "chdk": 0}, None, True, True),
    ("x", [OTHER_CANON], "subprocess", None, {"picamera2": None, "gphoto2": 1, "chdk": 0}, None, False, False),
    ("x", [], "picamera2", 2, {"picamera2": 2, "gphoto2": 0, "chdk": 0}, "picamera2", False, False),
    ("x", [OTHER_CANON], "gphoto2", None, {"picamera2": None, "gphoto2": 1, "chdk": 0}, None, False, False),
    ("x", [A2500], "gphoto2", None, {"picamera2": None, "gphoto2": 0, "chdk": 1}, None, False, True),
    (2, "x", "picamera2", None, {"picamera2": 2, "gphoto2": None, "chdk": None}, None, False, False),
    (0, "x", "picamera2", None, {"picamera2": 0, "gphoto2": None, "chdk": None}, None, False, False),
    (0, "x", "chdk", 2, {"picamera2": 0, "gphoto2": None, "chdk": 2}, None, False, False),
    (2, "x", "chdk", 0, {"picamera2": 2, "gphoto2": None, "chdk": 0}, None, False, True),
    ("x", "x", "chdk", None, {"picamera2": None, "gphoto2": None, "chdk": None}, None, False, False),
]


@pytest.mark.unit
@pytest.mark.parametrize("libcamera, canon, driver, held, connected, detected, mixed, mismatch", TRUTH)
def test_the_truth_table(monkeypatch, libcamera, canon, driver, held, connected, detected, mixed, mismatch):
    _probes(monkeypatch, libcamera=RuntimeError("x") if libcamera == "x" else libcamera,
            canon=ImportError("x") if canon == "x" else canon)
    monkeypatch.setattr(camera_kind, "_in_effect", {"kind": driver, "source": "environment"})
    backend = _HoldingBackend(driver, held=held) if held is not None else _Backend(driver)
    got = camera_kind.report(backend)
    assert got["connected"] == connected
    assert (got["detected"], got["mixed"], got["mismatch"]) == (detected, mixed, mismatch)


# ---------------------------------------------------------------------------
# GET /system/camera-kind
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("role", ["admin", "operator", "reviewer", "testing"])
def test_every_signed_in_role_reads_the_kind_without_a_backend(client, monkeypatch, role):
    from tests.unit.test_capture_manifest_export import _as

    built = []
    monkeypatch.setattr(capture_service, "get_backend", lambda: built.append(1))
    monkeypatch.setattr(capture_service, "_backend", None)
    camera_kind.store_choice("gphoto2")
    _effect(monkeypatch, "gphoto2", source="stored")
    _probes(monkeypatch, libcamera=0, canon=[OTHER_CANON, OTHER_CANON])
    resp = _as(client, role).get("/system/camera-kind")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"configured": "gphoto2", "source": "stored", "stored": "gphoto2",
                           "pending_restart": False, "detected": "gphoto2",
                           "connected": {"picamera2": 0, "gphoto2": 2, "chdk": 0},
                           "available": {"picamera2": True, "gphoto2": True, "chdk": True},
                           "mixed": False, "mismatch": False}
    assert built == []


@pytest.mark.unit
def test_the_kind_is_read_from_a_built_backend_when_the_scan_cannot_run(client, monkeypatch):
    _effect(monkeypatch, "chdk")
    monkeypatch.setattr(capture_service, "_backend", _HoldingBackend("chdk", held=2))
    _probes(monkeypatch, libcamera=0, canon=ImportError("no pychdk"))
    body = _admin(client).get("/system/camera-kind").json()
    assert body["connected"] == {"picamera2": 0, "gphoto2": None, "chdk": 2}


# ---------------------------------------------------------------------------
# Probe warnings
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_failing_probe_is_logged_once_and_again_when_the_reason_changes(monkeypatch, caplog):
    reasons = [ImportError("no module named pychdk")] * 3 + [OSError("no libusb backend")] * 2

    def usb():
        raise reasons.pop(0)

    monkeypatch.setattr(camera_kind, "_canon_product_ids", usb)
    with caplog.at_level(logging.WARNING, logger="app.core.camera_kind"):
        for _ in range(5):
            camera_kind.detect()
    lines = [r.getMessage() for r in caplog.records if "Canon USB" in r.getMessage()]
    assert len(lines) == 2
    assert "pychdk" in lines[0] and "libusb" in lines[1]


# ---------------------------------------------------------------------------
# PUT and DELETE /system/camera-kind
# ---------------------------------------------------------------------------

@pytest.fixture
def helper(monkeypatch):
    """The system helper: has restart-backend, and records each run."""
    from app.core import system_helper

    runs = []

    class H:
        supports = True
        ok = True

    monkeypatch.setattr(system_helper, "supports", lambda verb: H.supports)
    monkeypatch.setattr(system_helper, "run", lambda verb: runs.append(verb) or H.ok)
    H.runs = runs
    return H


def _admin(client):
    from tests.unit.test_capture_manifest_export import _as

    return _as(client, "admin", "admin1")


def _effect(monkeypatch, kind, source="detected"):
    """The kind in effect in this process; with source `stored`, the process started with that choice stored."""
    monkeypatch.setattr(capture_service.settings, "CAMERA_BACKEND", kind)
    monkeypatch.setattr(camera_kind, "_in_effect", {"kind": kind, "source": source})
    monkeypatch.setattr(camera_kind, "_applied_choice", kind if source == "stored" else None, raising=False)


@pytest.mark.unit
def test_put_stores_the_choice_and_restarts(client, monkeypatch, helper):
    from app.core import unit_activity

    _effect(monkeypatch, "picamera2")
    resp = _admin(client).put("/system/camera-kind", json={"kind": "GPhoto2 "})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"kind": "gphoto2", "configured": "picamera2", "restart": "restarting"}
    assert camera_kind.stored_choice() == "gphoto2"
    assert helper.runs == ["restart-backend"]
    assert unit_activity.restart_pending() is True


@pytest.mark.unit
def test_put_of_the_kind_in_effect_restarts_nothing(client, monkeypatch, helper):
    from app.core import unit_activity

    _effect(monkeypatch, "chdk")
    resp = _admin(client).put("/system/camera-kind", json={"kind": "chdk"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"kind": "chdk", "configured": "chdk", "restart": "not_needed"}
    assert helper.runs == []
    assert unit_activity.restart_pending() is False
    assert camera_kind.stored_choice() == "chdk"
    assert camera_kind.in_effect() == {"kind": "chdk", "source": "stored"}


@pytest.mark.unit
def test_put_while_a_capture_runs_is_refused_and_stores_nothing(client, monkeypatch, helper):
    from app.core import unit_activity

    _effect(monkeypatch, "picamera2")
    unit_activity.enter(unit_activity.CAMERA)
    try:
        resp = _admin(client).put("/system/camera-kind", json={"kind": "chdk"})
    finally:
        unit_activity.leave(unit_activity.CAMERA)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "busy_capturing"
    assert camera_kind.stored_choice() is None
    assert helper.runs == []
    assert unit_activity.restart_pending() is False


@pytest.mark.unit
def test_every_change_during_a_pending_restart_is_refused(client, monkeypatch, helper):
    """PUT chdk restarts; until that restart comes, a second PUT of any kind, the same kind or a DELETE stores nothing."""
    _effect(monkeypatch, "picamera2")
    api = _admin(client)
    assert api.put("/system/camera-kind", json={"kind": "chdk"}).json()["restart"] == "restarting"
    for method, body in (("put", {"kind": "gphoto2"}), ("put", {"kind": "picamera2"}),
                         ("put", {"kind": "chdk"}), ("delete", None)):
        resp = api.put("/system/camera-kind", json=body) if method == "put" else api.delete("/system/camera-kind")
        assert resp.status_code == 409, (method, body, resp.text)
        assert resp.json()["detail"]["code"] == "backend_restarting"
    assert camera_kind.stored_choice() == "chdk"
    assert helper.runs == ["restart-backend"]


@pytest.mark.unit
def test_a_store_that_cannot_be_written_releases_the_restart(client, monkeypatch, helper):
    from app.core import unit_activity

    _effect(monkeypatch, "picamera2")

    def fail(kind):
        raise OSError("read-only file system")

    monkeypatch.setattr(camera_kind, "store_choice", fail)
    resp = _admin(client).put("/system/camera-kind", json={"kind": "chdk"})
    assert resp.status_code == 500
    assert unit_activity.restart_pending() is False
    assert helper.runs == []


@pytest.mark.unit
def test_put_without_the_helper_verb_is_stored_and_restart_needed(client, monkeypatch, helper):
    from app.core import unit_activity

    helper.supports = False
    _effect(monkeypatch, "picamera2")
    resp = _admin(client).put("/system/camera-kind", json={"kind": "chdk"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["restart"] == "restart_needed"
    assert camera_kind.stored_choice() == "chdk"
    assert unit_activity.restart_pending() is False


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["subprocess", "nikon", "", 3])
def test_put_of_an_unknown_kind_is_422(client, monkeypatch, helper, kind):
    _effect(monkeypatch, "picamera2")
    resp = _admin(client).put("/system/camera-kind", json={"kind": kind})
    assert resp.status_code == 422, resp.text
    assert camera_kind.stored_choice() is None
    assert helper.runs == []


@pytest.mark.unit
@pytest.mark.parametrize("role", ["operator", "reviewer", "testing"])
def test_only_an_administrator_may_change_the_kind(client, monkeypatch, helper, role):
    from tests.unit.test_capture_manifest_export import _as

    camera_kind.store_choice("gphoto2")
    _effect(monkeypatch, "gphoto2", source="stored")
    api = _as(client, role)
    assert api.put("/system/camera-kind", json={"kind": "chdk"}).status_code == 403
    assert api.delete("/system/camera-kind").status_code == 403
    assert camera_kind.stored_choice() == "gphoto2"
    assert helper.runs == []


@pytest.mark.unit
def test_delete_clears_the_choice_and_restarts(client, monkeypatch, helper):
    camera_kind.store_choice("gphoto2")
    camera_kind.record_last_used("gphoto2")
    _effect(monkeypatch, "gphoto2", source="stored")
    resp = _admin(client).delete("/system/camera-kind")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"kind": None, "configured": "gphoto2", "restart": "restarting"}
    assert camera_kind.stored_choice() is None
    assert camera_kind.last_used() == "gphoto2"
    assert helper.runs == ["restart-backend"]


@pytest.mark.unit
def test_delete_with_no_choice_stored_restarts_nothing(client, monkeypatch, helper):
    _effect(monkeypatch, "picamera2")
    resp = _admin(client).delete("/system/camera-kind")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"kind": None, "configured": "picamera2", "restart": "not_needed"}
    assert helper.runs == []


@pytest.mark.unit
def test_delete_while_a_capture_runs_is_refused(client, monkeypatch, helper):
    from app.core import unit_activity

    camera_kind.store_choice("gphoto2")
    _effect(monkeypatch, "gphoto2", source="stored")
    unit_activity.enter(unit_activity.CAMERA)
    try:
        resp = _admin(client).delete("/system/camera-kind")
    finally:
        unit_activity.leave(unit_activity.CAMERA)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "busy_capturing"
    assert camera_kind.stored_choice() == "gphoto2"


@pytest.mark.unit
def test_a_backend_built_before_start_up_resolves_the_kind_first(monkeypatch):
    """Code that builds the backend without the lifespan (the hardware tests, a script) gets the same order, not a refusal."""
    _explicit(monkeypatch, None)
    _probes(monkeypatch, canon=[OTHER_CANON])
    assert capture_service.assert_camera_backend_configured() == "gphoto2"
    assert capture_service.settings.CAMERA_BACKEND == "gphoto2"
    assert camera_kind.in_effect() == {"kind": "gphoto2", "source": "detected"}


# ---------------------------------------------------------------------------
# A change waiting for its restart
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_put_whose_restart_failed_shows_as_pending(client, monkeypatch, helper):
    helper.ok = False
    _effect(monkeypatch, "picamera2")
    monkeypatch.setattr(capture_service, "_backend", None)
    api = _admin(client)
    assert api.put("/system/camera-kind", json={"kind": "chdk"}).json()["restart"] == "restart_needed"
    body = api.get("/system/camera-kind").json()
    assert body["configured"] == "picamera2"
    assert body["stored"] == "chdk"
    assert body["pending_restart"] is True


@pytest.mark.unit
def test_a_start_that_applied_the_choice_has_nothing_pending(monkeypatch):
    app_main, scans = _stub_lifespan(monkeypatch)
    _explicit(monkeypatch, "picamera2")
    camera_kind.store_choice("chdk")
    _serve(app_main)
    got = camera_kind.report(None)
    assert got["configured"] == "chdk"
    assert got["stored"] == "chdk"
    assert got["pending_restart"] is False


@pytest.mark.unit
def test_with_no_choice_stored_nothing_is_pending(client, monkeypatch, helper):
    from tests.unit.test_capture_manifest_export import _as

    _effect(monkeypatch, "gphoto2")
    _probes(monkeypatch)
    monkeypatch.setattr(capture_service, "get_backend", lambda: _Backend("gphoto2"))
    body = _as(client, "operator").get("/cameras/capabilities").json()
    assert body["stored"] is None
    assert body["pending_restart"] is False


# ---------------------------------------------------------------------------
# A kind whose driver this unit lacks
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("kind", ["picamera2", "gphoto2", "chdk"])
def test_put_of_a_kind_without_its_driver_is_refused(client, monkeypatch, helper, kind):
    from app.core import unit_activity

    _drivers(monkeypatch, **{kind: False})
    _effect(monkeypatch, "picamera2" if kind != "picamera2" else "gphoto2")
    resp = _admin(client).put("/system/camera-kind", json={"kind": kind})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "driver_unavailable"
    assert camera_kind.stored_choice() is None
    assert unit_activity.restart_pending() is False
    assert helper.runs == []


@pytest.mark.unit
def test_delete_is_allowed_whatever_drivers_are_missing(client, monkeypatch, helper):
    _drivers(monkeypatch, picamera2=False, gphoto2=False, chdk=False)
    camera_kind.store_choice("gphoto2")
    _effect(monkeypatch, "gphoto2", source="stored")
    resp = _admin(client).delete("/system/camera-kind")
    assert resp.status_code == 200, resp.text
    assert camera_kind.stored_choice() is None


# ---------------------------------------------------------------------------
# Another feature reserving a restart while the kind is written
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("same_kind", [True, False])
def test_no_restart_is_reserved_between_the_check_and_the_write(client, monkeypatch, helper, same_kind):
    """A pairing reservation tried while the PUT writes the store waits for the write, so the stored choice never changes during a reserved restart."""
    import threading

    from app.core import unit_activity

    _effect(monkeypatch, "chdk")
    seen = {}
    pairing = {}
    real = camera_kind.store_choice

    def store(kind):
        def reserve():
            pairing["reserved"] = unit_activity.reserve_restart()

        other = threading.Thread(target=reserve, daemon=True)
        other.start()
        other.join(timeout=0.5)
        seen["other_waited"] = other.is_alive()
        seen["pending_during_write"] = unit_activity.restart_pending()
        seen["thread"] = other
        real(kind)

    monkeypatch.setattr(camera_kind, "store_choice", store)
    resp = _admin(client).put("/system/camera-kind", json={"kind": "chdk" if same_kind else "gphoto2"})
    seen["thread"].join(timeout=5)
    assert not seen["thread"].is_alive(), "the other reservation never ended"
    assert resp.status_code == 200, resp.text
    if same_kind:
        # The PUT reserved nothing; the other reservation waited for the write and then took the restart.
        assert seen == {**seen, "other_waited": True, "pending_during_write": False}
        assert pairing["reserved"] is True
    else:
        # The PUT reserves its own restart after the write, in the same hold;
        # the other reservation, once let in, is refused.
        assert seen["other_waited"] is True
        assert seen["pending_during_write"] is False
        assert pairing["reserved"] is False


@pytest.mark.unit
def test_the_hold_refuses_while_a_restart_is_reserved_and_is_reentrant():
    from app.core import unit_activity

    with unit_activity.no_restart_reserved():
        assert unit_activity.reserve_restart() is True
    with pytest.raises(unit_activity.Refused):
        with unit_activity.no_restart_reserved():
            pass


# ---------------------------------------------------------------------------
# A pending change, measured against the choice this process started with
# ---------------------------------------------------------------------------

# (applied, stored before, request, helper works, stored after, answer, pending after)
# A is the kind in effect, B another kind.
CASES = [
    (None, None, ("put", "A"), True, "A", "not_needed", False),
    (None, None, ("put", "B"), True, "B", "restarting", True),
    (None, "B", ("put", "B"), False, "B", "restart_needed", True),
    (None, "B", ("put", "A"), True, "A", "not_needed", False),
    (None, "B", ("delete", None), True, None, "not_needed", False),
    (None, None, ("delete", None), True, None, "not_needed", False),
    ("A", "A", ("delete", None), False, None, "restart_needed", True),
    ("A", None, ("delete", None), True, None, "restarting", True),
    ("A", None, ("put", "A"), True, "A", "not_needed", False),
    ("A", "A", ("put", "B"), True, "B", "restarting", True),
    ("A", "A", ("put", "A"), True, "A", "not_needed", False),
]


@pytest.mark.unit
@pytest.mark.parametrize("applied, before, request_, works, after, answer, pending", CASES)
def test_the_cases_of_a_pending_change(client, monkeypatch, helper, applied, before, request_, works, after,
                                       answer, pending):
    from app.core import unit_activity

    names = {"A": "gphoto2", "B": "chdk", None: None}
    _effect(monkeypatch, "gphoto2", source="stored" if applied else "detected")
    monkeypatch.setattr(capture_service, "_backend", None)
    if names[before]:
        camera_kind.store_choice(names[before])
    helper.ok = works
    api = _admin(client)
    method, kind = request_
    resp = (api.put("/system/camera-kind", json={"kind": names[kind]}) if method == "put"
            else api.delete("/system/camera-kind"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["restart"] == answer
    assert camera_kind.stored_choice() == names[after]
    body = api.get("/system/camera-kind").json()
    assert body["stored"] == names[after]
    assert body["pending_restart"] is pending
    unit_activity.release_restart()


@pytest.mark.unit
def test_a_delete_whose_restart_failed_stays_pending_and_is_retried(client, monkeypatch, helper):
    """The reviewer's sequence: a stored choice applied at start, DELETE with the helper failing, then DELETE again."""
    camera_kind.store_choice("gphoto2")
    _effect(monkeypatch, "gphoto2", source="stored")
    monkeypatch.setattr(capture_service, "_backend", None)
    helper.ok = False
    api = _admin(client)
    assert api.delete("/system/camera-kind").json()["restart"] == "restart_needed"
    body = api.get("/system/camera-kind").json()
    assert (body["stored"], body["pending_restart"], body["configured"]) == (None, True, "gphoto2")
    helper.ok = True
    assert api.delete("/system/camera-kind").json()["restart"] == "restarting"
    assert helper.runs == ["restart-backend", "restart-backend"]


@pytest.mark.unit
def test_start_up_keeps_the_choice_it_read(monkeypatch):
    app_main, scans = _stub_lifespan(monkeypatch)
    _explicit(monkeypatch, None)
    camera_kind.store_choice("chdk")
    _serve(app_main)
    assert camera_kind.applied_choice() == "chdk"
    camera_kind.clear_choice()
    assert camera_kind.report(None)["pending_restart"] is True


# ---------------------------------------------------------------------------
# The reservation is taken after the write
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_slow_store_write_cannot_let_work_in_before_the_restart(client, monkeypatch, helper):
    """The store write takes longer than the reservation window; at the helper call the restart is still reserved and new camera work is refused."""
    from app.core import system_helper, unit_activity

    now = [1000.0]
    monkeypatch.setattr(unit_activity, "_clock", lambda: now[0])
    real = camera_kind.store_choice

    def slow(kind):
        now[0] += unit_activity.RESTART_RESERVATION_SECONDS + 60
        real(kind)

    monkeypatch.setattr(camera_kind, "store_choice", slow)
    at_helper = {}

    def run(verb):
        at_helper["pending"] = unit_activity.restart_pending()
        try:
            unit_activity.enter(unit_activity.CAMERA)
        except unit_activity.Refused:
            at_helper["camera"] = "refused"
        else:
            unit_activity.leave(unit_activity.CAMERA)
            at_helper["camera"] = "admitted"
        return True

    monkeypatch.setattr(system_helper, "run", run)
    _effect(monkeypatch, "picamera2")
    resp = _admin(client).put("/system/camera-kind", json={"kind": "chdk"})
    assert resp.json()["restart"] == "restarting"
    assert at_helper == {"pending": True, "camera": "refused"}


@pytest.mark.unit
def test_a_failed_write_reserves_nothing(client, monkeypatch, helper):
    from app.core import unit_activity

    reserved = []
    real = unit_activity.reserve_restart
    monkeypatch.setattr(unit_activity, "reserve_restart", lambda: reserved.append(1) or real())

    def fail(kind):
        raise OSError("read-only file system")

    monkeypatch.setattr(camera_kind, "store_choice", fail)
    _effect(monkeypatch, "picamera2")
    assert _admin(client).put("/system/camera-kind", json={"kind": "chdk"}).status_code == 500
    assert reserved == []
    assert unit_activity.restart_pending() is False


@pytest.mark.unit
def test_a_reservation_refused_after_the_write_puts_the_old_choice_back(client, monkeypatch, helper):
    from app.core import unit_activity

    camera_kind.store_choice("gphoto2")
    _effect(monkeypatch, "picamera2")
    monkeypatch.setattr(unit_activity, "reserve_restart", lambda: False)
    resp = _admin(client).put("/system/camera-kind", json={"kind": "chdk"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "backend_restarting"
    assert camera_kind.stored_choice() == "gphoto2"
    assert helper.runs == []
