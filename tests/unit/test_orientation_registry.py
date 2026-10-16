"""capture.camera_registry: persisted rotation per hardware id (NEH-71).

Orientation lives beside calibration in cameras.json, keyed by hardware id
so it survives reopening the app and rebooting the Pi, rather than being
sent fresh (and lost) with every capture request.

Also covers R30-1: two CameraRegistry instances are constructed per request
(app/api/cameras.py's _get_camera_registry), and each holds its own snapshot
of the file. Without a reload-modify-save discipline under a shared lock, an
instance that saves after another would silently clobber the other's change.
"""

import threading
import types

import pytest

from capture.camera_registry import CameraRegistry


def _register(registry, hw_id, camera_index=0, model="Canon EOS Rebel T7"):
    registry.register_resolved(
        hw_id,
        {"model": model, "serial": "1111111", "location": "USB usb:001,004"},
        camera_index,
    )


@pytest.mark.unit
def test_update_orientation_round_trips_through_a_fresh_registry_file(tmp_path):
    registry_path = tmp_path / "cameras.json"
    registry = CameraRegistry(registry_path=registry_path)
    _register(registry, "canoneosrebelt7_1111111", camera_index=0)

    registry.update_orientation("canoneosrebelt7_1111111", 270)

    reloaded = CameraRegistry(registry_path=registry_path)
    body = reloaded.get_camera_by_id("canoneosrebelt7_1111111")
    assert body["orientation"] == 270
    assert body["orientation_updated_at"]


@pytest.mark.unit
def test_update_orientation_rejects_an_invalid_degree_value(tmp_path):
    registry = CameraRegistry(registry_path=tmp_path / "cameras.json")
    _register(registry, "canoneosrebelt7_1111111")

    with pytest.raises(ValueError):
        registry.update_orientation("canoneosrebelt7_1111111", 45)


@pytest.mark.unit
def test_update_orientation_on_an_unknown_hardware_id_raises_key_error(tmp_path):
    registry = CameraRegistry(registry_path=tmp_path / "cameras.json")

    with pytest.raises(KeyError):
        registry.update_orientation("nonexistent_body", 90)


@pytest.mark.unit
def test_two_instances_writing_different_bodies_both_land(tmp_path):
    """R30-1: instance1 and instance2 are both constructed before either
    writes, then each mutates a different body. A third instance reading
    the file afterwards must see both changes, not just the last save."""
    registry_path = tmp_path / "cameras.json"
    seed = CameraRegistry(registry_path=registry_path)
    _register(seed, "body_a", camera_index=0)
    _register(seed, "body_b", camera_index=1)

    instance1 = CameraRegistry(registry_path=registry_path)
    instance2 = CameraRegistry(registry_path=registry_path)

    instance1.update_orientation("body_a", 90)
    instance2.update_calibration(
        "body_b", {"focus": {"success": True, "lens_position": 3.2}}
    )

    third = CameraRegistry(registry_path=registry_path)
    assert third.get_camera_by_id("body_a")["orientation"] == 90
    assert third.get_camera_by_id("body_b")["calibration"]["focus"]["lens_position"] == 3.2


@pytest.mark.unit
def test_second_writer_reloads_the_first_writers_save_before_applying_its_own(tmp_path):
    """R30-1 concurrency: the writer that loses the race for the lock must
    reload after the winner's save completes, not before - otherwise it
    would overwrite the winner's change with a stale copy."""
    registry_path = tmp_path / "cameras.json"
    seed = CameraRegistry(registry_path=registry_path)
    _register(seed, "body_a", camera_index=0)

    instance1 = CameraRegistry(registry_path=registry_path)
    instance2 = CameraRegistry(registry_path=registry_path)

    winner_holds_lock = threading.Event()
    let_winner_finish = threading.Event()
    observed = {}

    def slow_change(cameras):
        cameras["cameras"]["body_a"]["orientation"] = 90
        winner_holds_lock.set()
        # Unbounded on purpose: the test thread releases this gate in its
        # finally on every exit path, and a bounded wait would let writer 1
        # save and drop the lock on a slow runner before the mechanism
        # assertion below runs, failing correct code.
        let_winner_finish.wait()

    def checking_change(cameras):
        observed["orientation"] = cameras["cameras"]["body_a"].get("orientation")
        cameras["cameras"]["body_a"]["label"] = "left"

    errors = []

    def guarded(fn):
        try:
            fn()
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    from capture.camera_registry import _REGISTRY_WRITE_LOCK

    t1 = threading.Thread(target=guarded, args=(lambda: instance1._mutate(slow_change),))
    t2 = threading.Thread(target=guarded, args=(lambda: instance2._mutate(checking_change),))
    started = []
    try:
        t1.start()
        started.append(t1)
        assert winner_holds_lock.wait(timeout=2), "instance1 never entered its change callback"

        # Mechanism assertion, taken while instance1 is parked inside its
        # change callback: the process-wide write lock must be held right
        # now, or a second writer could reload before this save lands. The
        # final assertion below would pass on an unlocked reload-modify-save
        # whenever the scheduler happens to run instance2 after instance1.
        if _REGISTRY_WRITE_LOCK.acquire(blocking=False):
            _REGISTRY_WRITE_LOCK.release()
            raise AssertionError("the registry write lock was free during a mutation")

        t2.start()
        started.append(t2)
    finally:
        # Every exit path releases the gate and joins every started worker,
        # so a failed assertion above never leaves a thread holding the
        # global lock or writing the file after teardown. Unbounded joins:
        # the workers can only be blocked on the gate or on the lock.
        let_winner_finish.set()
        for thread in started:
            thread.join()

    assert not errors, errors
    assert observed["orientation"] == 90, (
        "instance2 reloaded before instance1's save completed"
    )


@pytest.mark.unit
def test_default_device_config_applies_the_saved_orientation():
    from capture.project_manager import default_camera_config_from_registry

    class _FakeRegistry:
        def get_camera_by_index(self, camera_index):
            return "hw", {"orientation": 270, "calibration": {}}

    config, hw_id = default_camera_config_from_registry(0, registry=_FakeRegistry())

    assert config["rotate_deg"] == 270
    assert hw_id == "hw"


@pytest.mark.unit
def test_default_device_config_leaves_rotate_deg_absent_when_never_set():
    from capture.project_manager import default_camera_config_from_registry

    class _FakeRegistry:
        def get_camera_by_index(self, camera_index):
            return "hw", {"orientation": None, "calibration": {}}

    config, hw_id = default_camera_config_from_registry(0, registry=_FakeRegistry())

    assert "rotate_deg" not in config
    assert hw_id == "hw"


@pytest.mark.unit
@pytest.mark.parametrize("stored", [45, True, "90", 90.0, None])
def test_a_stored_angle_outside_the_four_is_treated_as_unset(monkeypatch, stored):
    """A hand-edited or corrupt registry value must never reach the capture path."""
    from capture import project_manager

    assert CameraRegistry.is_valid_orientation(stored) is False
    fake = types.SimpleNamespace(
        get_camera_by_index=lambda idx: ("hw", {"orientation": stored, "calibration": {}})
    )
    config, hw_id = project_manager.default_camera_config_from_registry(0, registry=fake)
    assert "rotate_deg" not in config


@pytest.mark.unit
def test_device_rows_report_only_a_supported_angle():
    from app.api.cameras import _device_infos

    class _Reg:
        def __init__(self, stored):
            self.stored = stored

        def get_camera_by_id(self, hw_id):
            return {"orientation": self.stored, "calibration": {}}

    row = {"hardware_id": "hw", "index": 0, "model": "m"}
    assert _device_infos([row], _Reg(270))[0].orientation == 270
    assert _device_infos([row], _Reg(45))[0].orientation is None
    assert _device_infos([row], _Reg(True))[0].orientation is None
