"""The testing account copies a testing volume's pictures to a USB drive (NEH-391).

A. Which drives are offered (app.core.usb_copy.eligible_drives): an
   unmounted partition of a disk on the USB transport, formatted vfat or
   exFAT, on a disk none of whose partitions is mounted at `/`, at or below
   `/boot`, at, above or below a projects root, or under the mounts root,
   and not the drive the storage override names.
B. The three routes, for each role, on testing and real volumes; the
   device looked up again when the job starts; a thread that cannot start;
   the registry's memory of finished jobs.
C. The copy itself: the current images the volume's rows name, byte for
   byte, into a new folder `<stem>_<YYYYMMDD-HHMMSS>` on the drive, which
   the job always mounts and unmounts; previews only when a master is not
   a JPEG; nothing outside the volume's own directory read or probed.
D. Every failure code, with the drive unmounted afterwards.
E. One copy at a time; purge, clear, delete and moves of the volume while a
   copy runs; the administrator's storage routes refused on the copy's
   drive and unchanged otherwise; every file call the module makes inside
   the volume's directory or the mountpoint.
F. The system log.

No test runs sudo, mount or lsblk: lsblk's answer, the helper, the free
space, the allocation unit and the clock are stood in for
(usb_copy._run_lsblk, _run_helper, _free_bytes, _allocation_unit, _now),
and a temporary directory under a temporary mounts root is the drive.

    python -m pytest tests/unit/test_testing_space_usb.py

Version: 0.3.3
"""

import errno
import json
import os
import re
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_testing_space_capture import (  # noqa: F401  (backend, lookups and unit are fixtures)
    T, backend, lookups, unit,
)

# How long a test waits for a job or a gate before it fails.
WAIT_SECONDS = 30
START = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The drives, the helper, the free space and the clock, stood in for
# ---------------------------------------------------------------------------

class Drives:
    """lsblk's tree, the helper's mount and umount, the free space and allocation unit, and the clock."""

    def __init__(self, base: Path):
        self.base = base
        self.disks = []
        self.calls = []
        self.fail = set()
        self.free = 10 ** 12
        self.unit = 1
        self.lsblk_calls = 0
        self.on_lsblk = None
        self.lsblk_error = None
        self.gate = None
        self.gate_reached = threading.Event()
        self.ticks = 0
        self.extra_mounts = []
        self.mounts_error = None
        self.on_mount = None

    def disk(self, name, tran):
        node = {"name": name, "size": 32 * 10 ** 9, "fstype": None, "mountpoint": None, "label": None,
                "rm": tran == "usb", "type": "disk", "tran": tran, "pkname": None, "children": []}
        self.disks.append(node)
        return node

    def part(self, disk, name, fstype, label=None, mountpoint=None, size=16 * 10 ** 9):
        node = {"name": name, "size": size, "fstype": fstype, "mountpoint": mountpoint, "label": label,
                "rm": disk["rm"], "type": "part", "tran": None, "pkname": disk["name"]}
        disk["children"].append(node)
        return node

    def child(self, parent, name, kind, fstype=None, mountpoint=None):
        node = {"name": name, "size": 8 * 10 ** 9, "fstype": fstype, "mountpoint": mountpoint, "label": None,
                "rm": False, "type": kind, "tran": None, "pkname": parent["name"]}
        parent.setdefault("children", []).append(node)
        return node

    def find(self, name):
        for disk in self.disks:
            for part in disk["children"]:
                if part["name"] == name:
                    return part
        return None

    def lsblk(self):
        self.lsblk_calls += 1
        if self.on_lsblk is not None:
            self.on_lsblk(self)
        if self.lsblk_error is not None:
            raise self.lsblk_error
        return json.dumps({"blockdevices": self.disks})

    def helper(self, args):
        self.calls.append(list(args))
        verb = args[0]
        if verb in self.fail:
            return False, f"dtk-system-helper: {verb} failed"
        # The drive's files are kept in a directory of its own while it is unmounted, and the
        # mountpoint directory exists only while it is mounted, as the helper creates and removes it.
        if verb == "mount":
            device, mountpoint = args[1], args[2]
            name = device[len("/dev/"):]
            part = self.find(name)
            stash = self.stash(name)
            if stash.exists():
                stash.rename(mountpoint)
            else:
                os.makedirs(mountpoint)
            part["mountpoint"] = mountpoint
            if self.on_mount is not None:
                self.on_mount(self)
        elif verb == "umount":
            for disk in self.disks:
                for part in disk["children"]:
                    if part["mountpoint"] == args[1]:
                        part["mountpoint"] = None
                        Path(args[1]).rename(self.stash(part["name"]))
        return True, ""

    def mount_table(self):
        """/proc/self/mounts as the kernel would list lsblk's mounts, and the extra lines a test adds."""
        if self.mounts_error is not None:
            raise self.mounts_error
        lines = []
        stack = list(self.disks)
        while stack:
            node = stack.pop()
            stack.extend(node.get("children") or [])
            if node["mountpoint"] and node["mountpoint"] != "[SWAP]":
                where = node["mountpoint"].replace(" ", "\\040")
                lines.append(f"/dev/{node['name']} {where} {node['fstype']} rw,nosuid 0 0")
        return "\n".join(["proc /proc proc rw 0 0"] + lines + self.extra_mounts) + "\n"

    def stash(self, name):
        return self.base.parent / f"drive-{name}"

    def free_bytes(self, path):
        if self.gate is not None and threading.current_thread().name == "usb-copy":
            self.gate_reached.set()
            assert self.gate.wait(WAIT_SECONDS), "the gate was never opened"
        return self.free

    def allocation_unit(self, path):
        return self.unit

    def now(self):
        self.ticks += 1
        return START + timedelta(seconds=self.ticks - 1)


@pytest.fixture
def drives(monkeypatch, tmp_path, override_projects_root):
    from app.core import storage_override, usb_copy

    base = (tmp_path / "mounts").resolve()
    base.mkdir()
    d = Drives(base)
    monkeypatch.setattr(usb_copy, "MOUNT_BASE", base)
    monkeypatch.setattr(usb_copy, "_run_lsblk", d.lsblk)
    monkeypatch.setattr(usb_copy, "_run_helper", d.helper)
    monkeypatch.setattr(usb_copy, "_free_bytes", d.free_bytes)
    monkeypatch.setattr(usb_copy, "_allocation_unit", d.allocation_unit)
    monkeypatch.setattr(usb_copy, "_now", d.now)
    monkeypatch.setattr(usb_copy, "_read_mount_table", d.mount_table)
    monkeypatch.setattr(storage_override, "_OVERRIDE_FILE", tmp_path / "storage-override.json")
    card = d.disk("mmcblk0", None)
    d.part(card, "mmcblk0p1", "vfat", label="bootfs", mountpoint="/boot/firmware")
    d.part(card, "mmcblk0p2", "ext4", label="rootfs", mountpoint="/")
    stick = d.disk("sda", "usb")
    d.part(stick, "sda1", "vfat", label="STICK")
    yield d
    if d.gate is not None:
        d.gate.set()
    usb_copy.usb_copies.wait_all(WAIT_SECONDS)


@pytest.fixture
def override_file(monkeypatch, tmp_path):
    """The storage override file, moved to a temporary path; `write(root)` sets an override."""
    from app.core import storage_override

    path = tmp_path / "storage-override.json"
    monkeypatch.setattr(storage_override, "_OVERRIDE_FILE", path)

    class Override:
        def write(self, root):
            path.write_text(json.dumps({"projects_root": str(root)}))

        def corrupt(self):
            path.write_text("{not json")

    return Override()


def _names():
    from app.core import usb_copy
    return sorted(d["name"] for d in usb_copy.eligible_drives())


# ---------------------------------------------------------------------------
# The material
# ---------------------------------------------------------------------------

def _capture(unit, project, volume_id, dual=True):
    body = {"project_name": project["name"], "collection_id": volume_id}
    if not dual:
        body["camera_index"] = 0
    resp = unit.post("testing", "/cameras/capture/dual" if dual else "/cameras/capture", body)
    assert resp.status_code == 200 and resp.json()["success"], resp.text
    return resp.json()["record_id"]


def _volume(unit, captures=2, name="Tomo"):
    project = unit.project("Pruebas " + name)
    volume = unit.volume(project_id=project["id"], name=name)
    for _ in range(captures):
        _capture(unit, project, volume["id"])
    return project, volume


def _rows(unit, volume_id):
    from app.models.record import Record, RecordImage
    unit.db.expire_all()
    return unit.db.query(RecordImage).join(Record).filter(Record.collection_id == volume_id) \
        .order_by(RecordImage.id).all()


def _sources(unit, volume_id):
    """{path under the volume's directory: bytes} of every file the current image rows name that exists."""
    from app.core.paths import is_raw_suffix, preview_sidecar_of
    out = {}
    for row in _rows(unit, volume_id):
        if not row.is_current:
            continue
        master = Path(row.file_path)
        paths = [master] + ([preview_sidecar_of(master)] if is_raw_suffix(master) else [])
        for p in paths:
            if p.is_file():
                out[f"{p.parent.name}/{p.name}"] = p.read_bytes()
    return out


def _on_drive(drives, device="sda1", name="STICK"):
    root = drives.base / name
    if not root.exists():
        root = drives.stash(device)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _expected(unit, volume_id, folder):
    return {f"{folder}/{k}": v for k, v in _sources(unit, volume_id).items()}


def _start(unit, caller, volume_id, device="sda1"):
    return unit.post(caller, f"/collections/{volume_id}/usb-copy", {"device": device})


def _wait(job_id):
    from app.core import usb_copy
    job = usb_copy.usb_copies.wait(job_id, WAIT_SECONDS)
    assert job is not None and job.state in ("done", "failed"), job and job.to_dict()
    return job


def _run(unit, volume_id, caller="testing", device="sda1"):
    resp = _start(unit, caller, volume_id, device)
    assert resp.status_code == 202, resp.text
    job = _wait(resp.json()["job_id"])
    status = unit.get(caller, f"/collections/{volume_id}/usb-copy/status/{job.id}")
    assert status.status_code == 200, status.text
    return status.json()


def _code(resp, status, code):
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["code"] == code, resp.text


def _mount_calls(drives):
    return [c for c in drives.calls if c[0] in ("mount", "umount")]


def _mounted_and_unmounted(drives):
    mountpoint = str(drives.base / "STICK")
    return _mount_calls(drives) == [["mount", "/dev/sda1", mountpoint], ["umount", mountpoint]]


def _failed(status, code):
    assert status["state"] == "failed" and status["error"] == code, status


# ---------------------------------------------------------------------------
# A. Which drives are offered
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_unmounted_usb_vfat_or_exfat_partition_is_offered_with_its_fields(drives):
    from app.core import usb_copy
    big = drives.disk("sdb", "usb")
    drives.part(big, "sdb1", "exfat", label="BIG", size=64 * 10 ** 9)
    assert usb_copy.eligible_drives() == [
        {"name": "sda1", "label": "STICK", "size": 16 * 10 ** 9, "fstype": "vfat", "free_bytes": None},
        {"name": "sdb1", "label": "BIG", "size": 64 * 10 ** 9, "fstype": "exfat", "free_bytes": None},
    ]


@pytest.mark.unit
@pytest.mark.parametrize("where", ["mounts-root", "elsewhere"])
def test_a_mounted_partition_is_not_offered(drives, where):
    mountpoint = str(drives.base / "BIG") if where == "mounts-root" else "/media/pi/BIG"
    big = drives.disk("sdb", "usb")
    drives.part(big, "sdb1", "exfat", label="BIG", mountpoint=mountpoint)
    assert _names() == ["sda1"]


@pytest.mark.unit
@pytest.mark.parametrize("fstype", ["ext4", "ntfs", "btrfs", None])
def test_a_usb_drive_in_another_file_system_is_not_offered(drives, fstype):
    other = drives.disk("sdb", "usb")
    drives.part(other, "sdb1", fstype, label="OTRO")
    assert _names() == ["sda1"]


@pytest.mark.unit
@pytest.mark.parametrize("disk,part,tran", [
    ("nvme0n1", "nvme0n1p1", "nvme"), ("mmcblk1", "mmcblk1p1", None), ("mmcblk1", "mmcblk1p1", "mmc"),
    ("sdb", "sdb1", "sata"),
])
def test_a_partition_of_a_disk_not_on_usb_is_not_offered(drives, disk, part, tran):
    node = drives.disk(disk, tran)
    drives.part(node, part, "vfat", label="TARJETA")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_a_whole_disk_without_partitions_is_not_offered(drives):
    node = drives.disk("sdb", "usb")
    node["fstype"], node["label"] = "vfat", "SUPERFLOPPY"
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_the_system_disk_of_a_unit_booted_from_usb_is_not_offered(drives):
    system = drives.disk("sdb", "usb")
    drives.part(system, "sdb1", "vfat", label="bootfs", mountpoint="/boot/firmware")
    drives.part(system, "sdb2", "ext4", label="rootfs", mountpoint="/")
    drives.part(system, "sdb3", "vfat", label="EXTRA")
    assert _names() == ["sda1"]


@pytest.mark.unit
@pytest.mark.parametrize("mountpoint", ["/", "/boot", "/boot/firmware", "/boot/efi"])
def test_a_disk_with_a_partition_at_the_root_or_under_boot_is_not_offered(drives, mountpoint):
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "ext4", mountpoint=mountpoint)
    drives.part(node, "sdb2", "vfat", label="LIBRE")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_a_disk_with_a_partition_under_the_mounts_root_is_not_offered(drives):
    drives.part(drives.disks[1], "sda2", "ext4", label="ALMACEN", mountpoint=str(drives.base / "ALMACEN"))
    assert _names() == []


@pytest.mark.unit
def test_the_disk_that_holds_the_default_projects_root_is_not_offered(drives, monkeypatch):
    from app.core import usb_copy
    from app.core.config import settings
    default = Path(os.path.realpath(settings.default_projects_dir))
    monkeypatch.setattr(usb_copy, "MOUNT_BASE", default.parent / "mounts-elsewhere")
    for mountpoint in (str(default), str(default.parent), str(default / "sub")):
        node = drives.disk(f"sd{'bcd'[len(drives.disks) - 2]}", "usb")
        drives.part(node, node["name"] + "1", "ext4", label="PROYECTOS", mountpoint=mountpoint)
        drives.part(node, node["name"] + "2", "vfat", label="LIBRE")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_the_disk_that_holds_the_active_projects_root_is_not_offered(drives, override_file):
    root = drives.base / "DATOS" / "dtk-projects"
    root.mkdir(parents=True)
    override_file.write(root)
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "exfat", label="DATOS", mountpoint=str(drives.base / "DATOS"))
    drives.part(node, "sdb2", "vfat", label="LIBRE")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_the_device_the_storage_override_names_is_not_offered_even_unmounted(drives, override_file):
    # The override names <mounts>/DATOS/dtk-projects; the drive labelled DATOS is not mounted now.
    override_file.write(drives.base / "DATOS" / "dtk-projects")
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "vfat", label="DATOS")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_no_drive_is_offered_while_the_override_file_cannot_be_read(drives, override_file):
    override_file.corrupt()
    assert _names() == []


@pytest.mark.unit
def test_a_disk_whose_partition_the_mount_table_shows_at_the_projects_root_is_not_offered(drives):
    from app.core.config import settings
    default = os.path.realpath(settings.default_projects_dir)
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "ext4", label="PROYECTOS", mountpoint="/mnt/backup")
    drives.part(node, "sdb2", "vfat", label="LIBRE")
    drives.extra_mounts.append(f"/dev/sdb1 {default} ext4 rw 0 0")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_a_partition_the_mount_table_shows_mounted_is_not_offered(drives):
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "vfat", label="OTRO")
    drives.extra_mounts.append("/dev/sdb1 /media/pi/OTRO\\040DOS vfat rw 0 0")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_no_drive_is_offered_when_the_mount_table_cannot_be_read(drives):
    drives.mounts_error = OSError("no /proc")
    assert _names() == []


@pytest.mark.unit
def test_the_mount_table_is_read_from_proc(monkeypatch):
    from app.core import usb_copy
    assert usb_copy.MOUNT_TABLE == "/proc/self/mounts"
    assert usb_copy._mount_table_by_device("/dev/sda1 /media/pi/A\\040B vfat rw 0 0\n"
                                           "/dev/sda1 /mnt/bind vfat rw 0 0\nproc /proc proc rw 0 0\n") == {
        "sda1": {"/media/pi/A B", "/mnt/bind"}}


@pytest.mark.unit
def test_a_disk_with_a_swap_partition_is_not_offered(drives):
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "swap", mountpoint="[SWAP]")
    drives.part(node, "sdb2", "vfat", label="LIBRE")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_a_usb_system_disk_with_its_root_on_a_crypt_child_is_not_offered(drives):
    system = drives.disk("sdb", "usb")
    luks = drives.part(system, "sdb1", "crypto_LUKS")
    drives.child(luks, "cryptroot", "crypt", fstype="ext4", mountpoint="/")
    drives.part(system, "sdb2", "vfat", label="LIBRE")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_a_disk_with_an_lvm_child_is_not_offered(drives):
    node = drives.disk("sdb", "usb")
    member = drives.part(node, "sdb1", "LVM2_member")
    drives.child(member, "vg-datos", "lvm", fstype="ext4")
    drives.part(node, "sdb2", "vfat", label="LIBRE")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_a_disk_with_a_partition_that_has_children_is_not_offered(drives):
    node = drives.disk("sdb", "usb")
    outer = drives.part(node, "sdb1", "vfat", label="UNO")
    drives.child(outer, "sdb5", "part", fstype="vfat")
    drives.part(node, "sdb2", "vfat", label="DOS")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_the_override_naming_an_unmounted_partition_keeps_its_whole_disk(drives, override_file):
    override_file.write(drives.base / "DATOS" / "dtk-projects")
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "exfat", label="DATOS")
    drives.part(node, "sdb2", "vfat", label="LIBRE")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_a_stick_with_one_partition_mounted_by_an_administrator_offers_neither(drives):
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "vfat", label="UNO", mountpoint=str(drives.base / "UNO"))
    drives.part(node, "sdb2", "vfat", label="DOS")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_a_stick_with_two_unmounted_fat_partitions_offers_both(drives):
    node = drives.disk("sdb", "usb")
    drives.part(node, "sdb1", "vfat", label="UNO")
    drives.part(node, "sdb2", "exfat", label="DOS")
    drives.part(node, "sdb3", "ext4", label="TRES")
    assert _names() == ["sda1", "sdb1", "sdb2"]


@pytest.mark.unit
@pytest.mark.parametrize("with_partition", [False, True], ids=["superfloppy", "with-a-partition"])
def test_a_disk_node_that_is_itself_mounted_offers_nothing(drives, with_partition):
    node = drives.disk("sdb", "usb")
    node["fstype"], node["mountpoint"] = "vfat", str(drives.base / "FLOPPY")
    if with_partition:
        drives.part(node, "sdb1", "vfat", label="DENTRO")
    assert _names() == ["sda1"]


@pytest.mark.unit
def test_no_drive_is_offered_when_lsblk_cannot_be_read(drives):
    drives.lsblk_error = OSError("lsblk: not found")
    assert _names() == []


@pytest.mark.unit
def test_lsblk_is_asked_for_the_transport_and_the_parent(monkeypatch):
    from app.core import usb_copy
    seen = []

    class Done:
        returncode, stdout, stderr = 0, json.dumps({"blockdevices": []}), ""

    monkeypatch.setattr(usb_copy.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or Done())
    assert usb_copy.eligible_drives() == []
    (cmd, kw), = seen
    assert cmd[:3] == ["lsblk", "-J", "-b"] and cmd[3] == "-o"
    assert set(cmd[4].split(",")) == {"NAME", "SIZE", "FSTYPE", "MOUNTPOINT", "LABEL", "RM", "TYPE", "TRAN",
                                      "PKNAME"}
    assert kw["stdin"] is usb_copy.subprocess.DEVNULL and kw["timeout"]


@pytest.mark.unit
def test_the_helper_is_called_through_sudo_with_its_verbs(monkeypatch):
    from app.core import usb_copy
    from app.core.system_helper import HELPER
    seen = []

    class Done:
        returncode, stdout, stderr = 1, "", "dtk-system-helper: mount failed"

    monkeypatch.setattr(usb_copy.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or Done())
    assert usb_copy._run_helper(["mount", "/dev/sda1", "/var/lib/dtk/mounts/STICK"]) == (
        False, "dtk-system-helper: mount failed")
    (cmd, kw), = seen
    assert cmd == ["sudo", HELPER, "mount", "/dev/sda1", "/var/lib/dtk/mounts/STICK"]
    assert kw["stdin"] is usb_copy.subprocess.DEVNULL and kw["timeout"]


@pytest.mark.unit
def test_the_allocation_unit_is_read_from_statvfs(monkeypatch, tmp_path):
    from app.core import usb_copy

    class Vfs:
        f_frsize, f_bsize = 32768, 4096

    monkeypatch.setattr(usb_copy.os, "statvfs", lambda path: Vfs())
    assert usb_copy._allocation_unit(str(tmp_path)) == 32768


# ---------------------------------------------------------------------------
# B. The routes, for each role
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("caller,status", [("testing", 200), ("admin", 200), ("operator", 403),
                                           ("reviewer", 403)])
def test_the_drive_list_route(unit, drives, caller, status):
    resp = unit.get(caller, "/system/testing/usb")
    assert resp.status_code == status, resp.text
    if status == 200:
        assert resp.json() == {"drives": [{"name": "sda1", "label": "STICK", "size": 16 * 10 ** 9,
                                           "fstype": "vfat", "free_bytes": None}], "active": None}


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ["done", "failed"])
def test_the_drive_list_names_the_copy_that_holds_the_slot(unit, drives, outcome):
    for caller in ("testing", "admin"):
        assert unit.get(caller, "/system/testing/usb").json()["active"] is None
    if outcome == "failed":
        drives.fail.add("umount")
    _project, volume, job_id = _held_copy(unit, drives)
    for caller in ("testing", "admin"):
        resp = unit.get(caller, "/system/testing/usb")
        assert resp.status_code == 200, resp.text
        assert resp.json()["active"] == {"collection_id": volume["id"], "job_id": job_id}
    drives.gate.set()
    assert _wait(job_id).state == outcome
    for caller in ("testing", "admin"):
        assert unit.get(caller, "/system/testing/usb").json()["active"] is None


@pytest.mark.unit
@pytest.mark.parametrize("caller", ["testing", "admin"])
def test_the_testing_account_and_an_administrator_copy_a_testing_volume(unit, drives, caller):
    _project, volume = _volume(unit)
    status = _run(unit, volume["id"], caller=caller)
    assert status["state"] == "done" and status["error"] is None, status
    assert _on_drive(drives) == _expected(unit, volume["id"], status["folder"])


@pytest.mark.unit
def test_the_testing_account_names_a_real_volume_and_it_is_not_found(unit, drives):
    resp = _start(unit, "testing", unit.real_volume.id)
    _code(resp, 404, "not_found")
    assert drives.calls == [] and _on_drive(drives) == {}


@pytest.mark.unit
def test_an_administrator_naming_a_real_volume_is_refused_with_a_code(unit, drives):
    resp = _start(unit, "admin", unit.real_volume.id)
    _code(resp, 409, "usb_real_volume")
    assert drives.calls == [] and _on_drive(drives) == {}


@pytest.mark.unit
@pytest.mark.parametrize("caller", ["operator", "reviewer"])
def test_operators_and_reviewers_are_refused_every_usb_route(unit, drives, caller):
    _project, volume = _volume(unit, captures=1)
    for vid in (volume["id"], unit.real_volume.id):
        assert _start(unit, caller, vid).status_code == 403
        assert unit.get(caller, f"/collections/{vid}/usb-copy/status/x").status_code == 403
    assert drives.calls == []


@pytest.mark.unit
def test_a_status_is_not_found_for_another_volumes_job_or_an_unknown_one(unit, drives):
    _project, volume = _volume(unit, captures=1)
    other = unit.volume(project_id=_project["id"], name="Otro")
    resp = _start(unit, "testing", volume["id"])
    job_id = resp.json()["job_id"]
    _wait(job_id)
    assert unit.get("testing", f"/collections/{other['id']}/usb-copy/status/{job_id}").status_code == 404
    assert unit.get("testing", f"/collections/{volume['id']}/usb-copy/status/nope").status_code == 404
    assert unit.get("admin", f"/collections/{volume['id']}/usb-copy/status/{job_id}").status_code == 200


@pytest.mark.unit
@pytest.mark.parametrize("device", ["sdz1", "/dev/sda1", "mmcblk0p1", "mmcblk0p2", "../sda1", "sda1; rm", ""])
def test_a_device_not_in_the_list_is_refused_and_never_reaches_a_command(unit, drives, device):
    _project, volume = _volume(unit, captures=1)
    _code(_start(unit, "testing", volume["id"], device), 422, "usb_not_eligible")
    assert drives.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("change", ["unplugged", "another-drive", "mounted-meanwhile"])
def test_the_device_is_looked_up_again_when_the_job_starts(unit, drives, change):
    _project, volume = _volume(unit, captures=1)

    def swap_after_the_request(d):
        if d.lsblk_calls != 2:
            return
        if change == "unplugged":
            d.disks[1]["children"] = []
        elif change == "another-drive":
            d.disks[1]["children"] = []
            d.part(d.disks[1], "sda1", "vfat", label="OTRA", size=8 * 10 ** 9)
        else:
            d.find("sda1")["mountpoint"] = "/media/pi/STICK"

    drives.on_lsblk = swap_after_the_request
    status = _run(unit, volume["id"])
    _failed(status, "usb_not_eligible")
    assert drives.calls == [] and _on_drive(drives) == {}


@pytest.mark.unit
@pytest.mark.parametrize("change", ["label", "size", "fstype", "second-mountpoint", "system-sibling",
                                    "sibling-mounted", "child-appeared"])
def test_the_drive_is_checked_again_once_mounted(unit, drives, change):
    _project, volume = _volume(unit, captures=1)

    def swap_at_the_mount(d):
        part = d.find("sda1")
        if change == "label":
            part["label"] = "OTRA"
        elif change == "size":
            part["size"] = 8 * 10 ** 9
        elif change == "fstype":
            part["fstype"] = "ntfs"
        elif change == "second-mountpoint":
            d.extra_mounts.append("/dev/sda1 /media/pi/STICK vfat rw 0 0")
        elif change == "system-sibling":
            d.part(d.disks[1], "sda2", "ext4", mountpoint="/")
        elif change == "sibling-mounted":
            d.part(d.disks[1], "sda2", "vfat", label="OTRA", mountpoint="/media/pi/OTRA")
        else:
            d.child(d.disks[1], "sda-crypt", "crypt")

    drives.on_mount = swap_at_the_mount
    status = _run(unit, volume["id"])
    _failed(status, "usb_not_eligible")
    assert _mounted_and_unmounted(drives)
    assert _on_drive(drives) == {}


@pytest.mark.unit
def test_a_volume_a_purge_has_marked_is_not_found(unit, drives):
    from app.core import testing_space
    from app.models.collection import Collection
    _project, volume = _volume(unit, captures=1)
    row = unit.db.get(Collection, volume["id"])
    row.testing_expires_at = testing_space.REMOVING_AT
    unit.db.commit()
    _code(_start(unit, "testing", volume["id"]), 404, "not_found")
    assert drives.calls == []


@pytest.mark.unit
def test_a_thread_that_cannot_start_releases_the_hold_and_the_slot(unit, drives, monkeypatch):
    from fastapi.testclient import TestClient
    from app.core import usb_copy
    from app.core.export_jobs import export_jobs
    from app.main import app
    _project, volume = _volume(unit, captures=1)
    real_start = usb_copy._start_thread

    def no_thread(target, args):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(usb_copy, "_start_thread", no_thread)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(f"/collections/{volume['id']}/usb-copy", headers=unit.headers["testing"],
                       json={"device": "sda1"})
    assert resp.status_code == 500, resp.text
    assert usb_copy.usb_copies.running() is None
    assert not export_jobs.copying([volume["id"]])
    monkeypatch.setattr(usb_copy, "_start_thread", real_start)
    assert _run(unit, volume["id"])["state"] == "done"


@pytest.mark.unit
def test_the_registry_keeps_the_twenty_most_recent_finished_jobs(unit, drives):
    from app.core import usb_copy
    from app.models.collection import Collection
    _project, volume = _volume(unit, captures=1)
    registry = usb_copy.UsbCopyRegistry()
    row = unit.db.get(Collection, volume["id"])
    jobs = []
    for _ in range(usb_copy.KEEP_FINISHED + 1):
        job = registry.start(unit.db, row, "sda1", "u-testing", work=lambda job: None)
        assert registry.wait(job.id, WAIT_SECONDS).state == "done"
        jobs.append(job.id)
    assert usb_copy.KEEP_FINISHED == 20
    assert registry.get(jobs[0]) is None
    assert all(registry.get(j) is not None for j in jobs[1:])


# ---------------------------------------------------------------------------
# C. The copy
# ---------------------------------------------------------------------------

STATUS_KEYS = {"job_id", "collection_id", "state", "done", "total", "bytes_done", "bytes_total", "error",
               "message", "folder", "drive", "skipped", "unmounted"}


@pytest.mark.unit
def test_the_copy_writes_every_master_into_a_new_folder_and_mounts_and_unmounts(unit, drives):
    _project, volume = _volume(unit, captures=2)
    sources = _sources(unit, volume["id"])
    assert len(sources) == 4 and all(k.startswith("captures/") for k in sources)
    status = _run(unit, volume["id"])
    folder = f"{volume['filename_stem']}_20261120-120000"
    size = sum(map(len, sources.values()))
    assert set(status) == STATUS_KEYS
    assert status == {**status, "state": "done", "error": None, "folder": folder, "done": 4, "total": 4,
                      "bytes_total": size, "bytes_done": size, "unmounted": True, "drive": "STICK",
                      "skipped": {"not_regular": 0, "outside": 0, "missing": 0}}
    assert _on_drive(drives) == _expected(unit, volume["id"], folder)
    assert _mounted_and_unmounted(drives)


@pytest.mark.unit
def test_previews_are_copied_when_a_master_is_not_a_jpeg(unit, drives):
    unit.backend.raw = True
    _project, volume = _volume(unit, captures=1)
    sources = _sources(unit, volume["id"])
    assert {k.split("/")[0] for k in sources} == {"captures", "previews"}
    status = _run(unit, volume["id"])
    assert status["state"] == "done", status
    assert _on_drive(drives) == _expected(unit, volume["id"], status["folder"])


@pytest.mark.unit
def test_only_the_files_the_rows_name_are_copied_and_no_preview_of_an_all_jpeg_volume(unit, drives):
    from app.core.config import settings
    _project, volume = _volume(unit, captures=1)
    stem = volume["filename_stem"]
    directory = Path(settings.projects_dir) / stem
    (directory / "previews").mkdir(exist_ok=True)
    (directory / "previews" / f"{stem}-img_0001_c0_preview.jpg").write_bytes(b"a preview no row needs")
    (directory / "captures" / "suelto.jpg").write_bytes(b"a file no row names")
    status = _run(unit, volume["id"])
    assert status["state"] == "done", status
    assert _on_drive(drives) == _expected(unit, volume["id"], status["folder"])


def _supersede(unit, volume_id):
    """The first image row made superseded; its path."""
    rows = _rows(unit, volume_id)
    rows[0].is_current = False
    rows[0].superseded_at = START.replace(tzinfo=None)
    unit.db.commit()
    return rows[0].id, Path(rows[0].file_path)


@pytest.mark.unit
def test_a_superseded_image_is_not_copied(unit, drives):
    _project, volume = _volume(unit, captures=1)
    _image_id, superseded = _supersede(unit, volume["id"])
    status = _run(unit, volume["id"])
    assert status["state"] == "done" and status["total"] == 1, status
    names = [Path(k).name for k in _on_drive(drives)]
    assert names and superseded.name not in names


@pytest.mark.unit
def test_a_superseded_image_is_not_deleted_during_a_copy_and_the_copy_is_unchanged(unit, drives):
    drives.gate = threading.Event()
    _project, volume = _volume(unit, captures=1)
    image_id, superseded = _supersede(unit, volume["id"])
    expected = _sources(unit, volume["id"])
    resp = _start(unit, "testing", volume["id"])
    assert resp.status_code == 202, resp.text
    assert drives.gate_reached.wait(WAIT_SECONDS)
    deleted = unit.client.delete(f"/records/images/{image_id}", headers=unit.headers["testing"])
    assert deleted.status_code == 409, deleted.text
    assert superseded.is_file()
    drives.gate.set()
    job = _wait(resp.json()["job_id"])
    assert job.state == "done" and job.done == 1, job.to_dict()
    assert _on_drive(drives) == {f"{job.to_dict()['folder']}/{k}": v for k, v in expected.items()}
    deleted = unit.client.delete(f"/records/images/{image_id}", headers=unit.headers["testing"])
    assert deleted.status_code == 200, deleted.text
    assert not superseded.exists()


@pytest.mark.unit
def test_each_copy_writes_a_new_folder(unit, drives):
    _project, volume = _volume(unit, captures=1)
    first = _run(unit, volume["id"])
    second = _run(unit, volume["id"])
    assert first["state"] == second["state"] == "done"
    assert first["folder"] != second["folder"]
    for status in (first, second):
        assert re.fullmatch(re.escape(volume["filename_stem"]) + r"_\d{8}-\d{6}", status["folder"])
        assert status["done"] == 2
    assert _on_drive(drives) == {**_expected(unit, volume["id"], first["folder"]),
                                 **_expected(unit, volume["id"], second["folder"])}


@pytest.mark.unit
def test_usb_exists_when_the_folder_is_already_on_the_drive(unit, drives):
    _project, volume = _volume(unit, captures=1)
    folder = f"{volume['filename_stem']}_20261120-120000"
    theirs = drives.stash("sda1") / folder / "nota.txt"
    theirs.parent.mkdir(parents=True)
    theirs.write_bytes(b"someone else's file")
    status = _run(unit, volume["id"])
    _failed(status, "usb_exists")
    assert _on_drive(drives) == {f"{folder}/nota.txt": b"someone else's file"}
    assert _mounted_and_unmounted(drives)


@pytest.mark.unit
def test_a_symlinked_source_is_not_followed_and_is_counted(unit, drives, tmp_path):
    _project, volume = _volume(unit, captures=1)
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"outside the volume")
    master = Path(_rows(unit, volume["id"])[0].file_path)
    master.unlink()
    master.symlink_to(secret)
    status = _run(unit, volume["id"])
    assert status["state"] == "done" and status["skipped"]["not_regular"] == 1, status
    assert b"outside the volume" not in b"".join(_on_drive(drives).values())
    assert len(_on_drive(drives)) == 1


@pytest.mark.unit
def test_a_row_naming_a_file_outside_the_projects_root_is_not_read(unit, drives, tmp_path):
    _project, volume = _volume(unit, captures=1)
    secret = tmp_path / "captures" / "secret.jpg"
    secret.parent.mkdir()
    secret.write_bytes(b"outside the volume")
    row = _rows(unit, volume["id"])[0]
    row.file_path = str(secret)
    unit.db.commit()
    status = _run(unit, volume["id"])
    assert status["state"] == "done" and status["skipped"]["outside"] == 1, status
    assert b"outside the volume" not in b"".join(_on_drive(drives).values())


# ---------------------------------------------------------------------------
# D. The failure codes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_usb_mount_when_the_helper_cannot_mount(unit, drives):
    drives.fail.add("mount")
    _project, volume = _volume(unit, captures=1)
    _failed(_run(unit, volume["id"]), "usb_mount")
    assert [c[0] for c in drives.calls] == ["mount"]


@pytest.mark.unit
def test_usb_full_counts_each_file_in_whole_allocation_units(unit, drives):
    from app.core import usb_copy
    _project, volume = _volume(unit, captures=1)
    sizes = [len(v) for v in _sources(unit, volume["id"]).values()]
    drives.unit = 32768
    # Enough for the bytes themselves, not for two whole clusters.
    drives.free = sum(sizes) + usb_copy.FREE_MARGIN_BYTES + 32768
    _failed(_run(unit, volume["id"]), "usb_full")
    assert usb_copy.FREE_MARGIN_BYTES == 64 * 1024 * 1024
    assert _on_drive(drives) == {}
    assert _mounted_and_unmounted(drives)


@pytest.mark.unit
def test_whole_allocation_units_plus_the_margin_are_enough(unit, drives):
    from app.core import usb_copy
    _project, volume = _volume(unit, captures=1)
    drives.unit = 32768
    drives.free = 2 * 32768 + usb_copy.FREE_MARGIN_BYTES
    assert _run(unit, volume["id"])["state"] == "done"


class FsRecorder:
    """usb_copy's `os`, stood in for: every file call recorded; a write, a read or an open can be made to fail."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.fail_on_write = None
        self.write_errno = errno.EIO
        self.fail_on_read = None
        self.missing_on_open = None
        self.after_mkdir = None
        self.part_fds = set()
        self.source_fds = set()
        self.writes = 0
        self.path = _PathRecorder(real.path, self.calls)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def open(self, path, flags, *args, **kwargs):
        self.calls.append(("open", str(path), flags))
        if self.missing_on_open is not None and str(path) == self.missing_on_open:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        fd = self._real.open(path, flags, *args, **kwargs)
        if str(path).endswith(".part"):
            self.part_fds.add(fd)
        elif not flags & (self._real.O_WRONLY | self._real.O_RDWR):
            self.source_fds.add(fd)
        return fd

    def lstat(self, path, *args, **kwargs):
        self.calls.append(("lstat", str(path)))
        return self._real.lstat(path, *args, **kwargs)

    def stat(self, path, *args, **kwargs):
        self.calls.append(("stat", str(path)))
        return self._real.stat(path, *args, **kwargs)

    def rename(self, src, dst, *args, **kwargs):
        self.calls.append(("rename", str(src), str(dst)))
        return self._real.rename(src, dst, *args, **kwargs)

    def replace(self, src, dst, *args, **kwargs):
        self.calls.append(("replace", str(src), str(dst)))
        return self._real.replace(src, dst, *args, **kwargs)

    def unlink(self, path, *args, **kwargs):
        self.calls.append(("unlink", str(path)))
        return self._real.unlink(path, *args, **kwargs)

    def remove(self, path, *args, **kwargs):
        self.calls.append(("remove", str(path)))
        return self._real.remove(path, *args, **kwargs)

    def mkdir(self, path, *args, **kwargs):
        self.calls.append(("mkdir", str(path)))
        result = self._real.mkdir(path, *args, **kwargs)
        if self.after_mkdir is not None:
            self.after_mkdir(str(path))
        return result

    def makedirs(self, path, *args, **kwargs):
        self.calls.append(("makedirs", str(path)))
        return self._real.makedirs(path, *args, **kwargs)

    def rmdir(self, path, *args, **kwargs):
        self.calls.append(("rmdir", str(path)))
        return self._real.rmdir(path, *args, **kwargs)

    def write(self, fd, data):
        if fd in self.part_fds:
            self.writes += 1
            if self.fail_on_write is not None and self.writes == self.fail_on_write:
                raise OSError(self.write_errno, os.strerror(self.write_errno))
        return self._real.write(fd, data)

    def read(self, fd, n):
        if fd in self.source_fds and self.fail_on_read is not None:
            raise OSError(self.fail_on_read, os.strerror(self.fail_on_read))
        return self._real.read(fd, n)

    def close(self, fd):
        self.part_fds.discard(fd)
        self.source_fds.discard(fd)
        return self._real.close(fd)


class _PathRecorder:
    """os.path, with the probes recorded: exists, lexists, isfile, isdir, islink, getsize."""

    def __init__(self, real, calls):
        self._real = real
        self._calls = calls

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name in ("exists", "lexists", "isfile", "isdir", "islink", "getsize"):
            def recorded(path, *args, **kwargs):
                self._calls.append((name, str(path)))
                return attr(path, *args, **kwargs)
            return recorded
        return attr


@pytest.fixture
def fs(monkeypatch):
    from app.core import usb_copy
    recorder = FsRecorder(os)
    monkeypatch.setattr(usb_copy, "os", recorder)
    return recorder


def _paths_touched(fs):
    return [p for call in fs.calls for p in call[1:3] if isinstance(p, str)]


@pytest.mark.unit
def test_usb_write_on_an_io_error_mid_file_leaves_no_part_and_unmounts(unit, drives, fs, monkeypatch):
    from app.core import usb_copy
    monkeypatch.setattr(usb_copy, "CHUNK", 4)
    fs.fail_on_write = 3
    _project, volume = _volume(unit, captures=1)
    _failed(_run(unit, volume["id"]), "usb_write")
    assert not any(n.endswith(".part") for n in _on_drive(drives))
    assert _mounted_and_unmounted(drives)
    unlinked = [c[1] for c in fs.calls if c[0] in ("unlink", "remove")]
    assert len(unlinked) == 1 and unlinked[0].endswith(".part")


@pytest.mark.unit
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT], ids=["ENOSPC", "EDQUOT"])
def test_a_full_drive_while_writing_is_usb_full(unit, drives, fs, monkeypatch, code):
    from app.core import usb_copy
    monkeypatch.setattr(usb_copy, "CHUNK", 4)
    fs.fail_on_write, fs.write_errno = 2, code
    _project, volume = _volume(unit, captures=1)
    _failed(_run(unit, volume["id"]), "usb_full")
    assert not any(n.endswith(".part") for n in _on_drive(drives))
    assert _mounted_and_unmounted(drives)


@pytest.mark.unit
def test_a_part_that_exists_in_the_new_folder_is_usb_write(unit, drives, fs):
    _project, volume = _volume(unit, captures=1)
    name = sorted(_sources(unit, volume["id"]))[0].split("/")[1]

    def plant(path):
        if path.endswith(os.sep + "captures"):
            Path(path, name + ".part").write_bytes(b"appeared meanwhile")

    fs.after_mkdir = plant
    _failed(_run(unit, volume["id"]), "usb_write")
    assert _mounted_and_unmounted(drives)


@pytest.mark.unit
def test_a_final_name_that_exists_in_the_new_folder_is_usb_write(unit, drives, fs):
    _project, volume = _volume(unit, captures=1)
    name = sorted(_sources(unit, volume["id"]))[0].split("/")[1]

    def plant(path):
        if path.endswith(os.sep + "captures"):
            Path(path, name).write_bytes(b"appeared meanwhile")

    fs.after_mkdir = plant
    _failed(_run(unit, volume["id"]), "usb_write")
    on_drive = _on_drive(drives)
    assert [v for k, v in on_drive.items() if k.endswith("/" + name)] == [b"appeared meanwhile"]


@pytest.mark.unit
@pytest.mark.parametrize("how", ["open", "read"])
def test_usb_read_when_a_source_cannot_be_opened_or_read(unit, drives, fs, how):
    _project, volume = _volume(unit, captures=1)
    master = Path(_rows(unit, volume["id"])[0].file_path)
    if how == "open":
        master.chmod(0)
    else:
        fs.fail_on_read = errno.EIO
    try:
        status = _run(unit, volume["id"])
    finally:
        master.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _failed(status, "usb_read")
    assert _mounted_and_unmounted(drives)
    assert not any(n.endswith(".part") for n in _on_drive(drives))


@pytest.mark.unit
def test_a_source_gone_at_open_is_counted_missing_and_the_copy_goes_on(unit, drives, fs):
    _project, volume = _volume(unit, captures=1)
    rows = _rows(unit, volume["id"])
    fs.missing_on_open = os.path.normpath(rows[0].file_path)
    status = _run(unit, volume["id"])
    assert status["state"] == "done" and status["skipped"]["missing"] == 1 and status["done"] == 1, status
    assert [Path(k).name for k in _on_drive(drives)] == [Path(rows[1].file_path).name]


@pytest.mark.unit
def test_usb_name_for_a_stored_name_fat_cannot_hold(unit, drives):
    from app.core.config import settings
    _project, volume = _volume(unit, captures=1)
    row = _rows(unit, volume["id"])[0]
    old = Path(row.file_path)
    new = old.with_name("folio:1?.jpg")
    old.rename(new)
    row.file_path = str(new)
    unit.db.commit()
    assert new.is_relative_to(Path(settings.projects_dir))
    _failed(_run(unit, volume["id"]), "usb_name")
    assert _on_drive(drives) == {}
    assert drives.calls == []


@pytest.mark.unit
def test_usb_name_for_two_names_that_differ_only_in_case(unit, drives):
    _project, volume = _volume(unit, captures=1)
    rows = _rows(unit, volume["id"])
    # Only the row is changed: the check reads the stored names before any file.
    first = Path(rows[0].file_path)
    rows[1].file_path = str(first.with_name(first.stem.upper() + first.suffix))
    unit.db.commit()
    _failed(_run(unit, volume["id"]), "usb_name")


@pytest.mark.unit
def test_usb_unmount_is_reported_once_and_not_retried(unit, drives):
    drives.fail.add("umount")
    _project, volume = _volume(unit, captures=1)
    status = _run(unit, volume["id"])
    _failed(status, "usb_unmount")
    assert status["unmounted"] is False
    assert [c[0] for c in _mount_calls(drives)] == ["mount", "umount"]
    assert len(_on_drive(drives)) == 2


@pytest.mark.unit
def test_the_drive_is_not_unmounted_when_the_override_lies_on_it(unit, drives, override_file):
    drives.gate = threading.Event()
    _project, volume = _volume(unit, captures=1)
    resp = _start(unit, "testing", volume["id"])
    assert resp.status_code == 202, resp.text
    assert drives.gate_reached.wait(WAIT_SECONDS)
    override_file.write(drives.base / "STICK" / "dtk-projects")
    drives.gate.set()
    job = _wait(resp.json()["job_id"])
    assert job.state == "failed" and job.error == "usb_unmount", job.to_dict()
    assert [c[0] for c in _mount_calls(drives)] == ["mount"]


@pytest.mark.unit
def test_usb_volume_empty_mounts_nothing(unit, drives):
    project = unit.project("Vacío")
    volume = unit.volume(project_id=project["id"], name="Vacío")
    _failed(_run(unit, volume["id"]), "usb_volume_empty")
    assert drives.calls == []


@pytest.mark.unit
def test_a_volume_whose_files_are_all_gone_is_empty(unit, drives):
    _project, volume = _volume(unit, captures=1)
    for row in _rows(unit, volume["id"]):
        Path(row.file_path).unlink()
    _failed(_run(unit, volume["id"]), "usb_volume_empty")
    assert drives.calls == []


# ---------------------------------------------------------------------------
# E. One copy at a time, the volume and the drive held while it runs
# ---------------------------------------------------------------------------

def _held_copy(unit, drives):
    """A copy started and held at its free-space reading, after the mount: (project, volume, job id)."""
    drives.gate = threading.Event()
    project, volume = _volume(unit, captures=1)
    resp = _start(unit, "testing", volume["id"])
    assert resp.status_code == 202, resp.text
    assert drives.gate_reached.wait(WAIT_SECONDS)
    return project, volume, resp.json()["job_id"]


@pytest.mark.unit
def test_one_copy_at_a_time(unit, drives):
    _project, volume, job_id = _held_copy(unit, drives)
    _other_project, other = _volume(unit, captures=1, name="Otro")
    _code(_start(unit, "testing", other["id"]), 409, "usb_copy_busy")
    _code(_start(unit, "admin", volume["id"]), 409, "usb_copy_busy")
    drives.gate.set()
    assert _wait(job_id).state == "done"
    assert _run(unit, other["id"])["state"] == "done"


@pytest.mark.unit
def test_purge_clear_delete_and_moves_leave_a_volume_being_copied(unit, drives):
    from app.core import testing_space
    from app.models.collection import Collection
    project, volume, job_id = _held_copy(unit, drives)
    sources = _sources(unit, volume["id"])

    assert testing_space.purge_volume(unit.db, unit.db.get(Collection, volume["id"])) is False
    later = testing_space._now() + timedelta(days=4000)
    assert testing_space.purge_expired(unit.db, later)["skipped"] >= 1
    result = testing_space.clear(unit.db, actor="u-admin")
    assert result["skipped"] >= 1 and result["volumes"] == 0, result
    resp = unit.client.delete(f"/collections/{volume['id']}", headers=unit.headers["testing"])
    _code(resp, 409, "testing_volume_busy")
    assert "USB" in resp.json()["detail"]["message"]
    target = unit.volume(project_id=project["id"], name="Destino")
    resp = unit.post("testing", f"/collections/{volume['id']}/move-records",
                     params={"target_collection_id": target["id"]})
    assert resp.status_code == 409, resp.text
    assert _sources(unit, volume["id"]) == sources
    unit.db.expire_all()
    assert unit.db.get(Collection, volume["id"]).testing_expires_at != testing_space.REMOVING_AT

    drives.gate.set()
    assert _wait(job_id).state == "done"
    assert testing_space.purge_volume(unit.db, unit.db.get(Collection, volume["id"])) is True


@pytest.mark.unit
def test_with_no_copy_a_superseded_image_is_deleted_as_before(unit, drives):
    _project, volume = _volume(unit, captures=1)
    for caller, volume_id in (("operator", unit.real_volume.id), ("testing", volume["id"])):
        image_id, path = _supersede(unit, volume_id)
        assert path.is_file()
        resp = unit.client.delete(f"/records/images/{image_id}", headers=unit.headers[caller])
        assert resp.status_code == 200, (caller, resp.text)
        assert not path.exists()


@pytest.mark.unit
def test_an_image_a_retake_superseded_during_a_copy_is_not_deleted_under_it(unit, drives):
    from app.models.record import Record
    project, volume, job_id = _held_copy(unit, drives)
    first = _rows(unit, volume["id"])[0]
    image_id, path, record_id = first.id, Path(first.file_path), first.record_id
    unit.db.get(Record, record_id).status = "rejected"
    unit.db.commit()
    retake = unit.post("testing", "/cameras/capture/dual",
                       {"project_name": project["name"], "collection_id": volume["id"], "record_id": record_id})
    assert retake.status_code == 200 and retake.json()["success"], retake.text
    unit.db.expire_all()
    assert not [r for r in _rows(unit, volume["id"]) if r.id == image_id][0].is_current
    resp = unit.client.delete(f"/records/images/{image_id}", headers=unit.headers["testing"])
    assert resp.status_code == 409, resp.text
    assert path.is_file()
    drives.gate.set()
    job = _wait(job_id)
    assert job.state == "done" and job.done == 2, job.to_dict()
    resp = unit.client.delete(f"/records/images/{image_id}", headers=unit.headers["testing"])
    assert resp.status_code == 200, resp.text
    assert not path.exists()


@pytest.mark.unit
def test_a_record_delete_during_a_copy_is_refused(unit, drives):
    _project, volume, job_id = _held_copy(unit, drives)
    record_id = _rows(unit, volume["id"])[0].record_id
    resp = unit.client.delete(f"/records/{record_id}", headers=unit.headers["testing"])
    assert resp.status_code == 409, resp.text
    drives.gate.set()
    assert _wait(job_id).state == "done"
    assert unit.client.delete(f"/records/{record_id}", headers=unit.headers["testing"]).status_code == 200


@pytest.mark.unit
def test_no_copy_starts_while_a_request_changes_the_volumes_files(unit, drives):
    from app.core.export_jobs import export_jobs
    _project, volume = _volume(unit, captures=1)
    with export_jobs.hold_for_file_changes([volume["id"]]):
        _code(_start(unit, "testing", volume["id"]), 409, "testing_volume_busy")
    assert drives.calls == []


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.fixture
def storage_routes(monkeypatch, drives):
    """The administrator's storage routes with their commands recorded: lsblk's list, the helper, the override."""
    import app.api.system as system
    from app.core import storage_override

    recorded = {"run": [], "override": []}
    partitions = [
        {"name": "sda1", "path": "/dev/sda1", "size": "16G", "fstype": "vfat", "mountpoint": None,
         "label": "STICK", "removable": True, "type": "part"},
        {"name": "sdb1", "path": "/dev/sdb1", "size": "16G", "fstype": "vfat", "mountpoint": None,
         "label": "OTRA", "removable": True, "type": "part"},
    ]
    monkeypatch.setattr(system, "_parse_lsblk", lambda: partitions)
    monkeypatch.setattr(system, "_MOUNT_BASE", drives.base)
    # The route module's own reference to subprocess, so nothing else's commands are recorded.
    monkeypatch.setattr(system, "subprocess", SimpleNamespace(
        run=lambda cmd, **kw: recorded["run"].append(cmd) or _Result(0), DEVNULL=-3))
    monkeypatch.setattr(storage_override, "override_root_problem", lambda root: None)
    monkeypatch.setattr(storage_override, "set_storage_override", lambda path: recorded["override"].append(path))
    recorded["partitions"] = partitions
    return recorded


def _admin_storage(unit, method, path, body):
    return unit.client.request(method, path, headers=unit.headers["admin"], json=body)


@pytest.mark.unit
def test_the_storage_routes_refuse_the_drive_a_copy_holds(unit, drives, storage_routes):
    from app.core.system_helper import HELPER
    _project, _volume_, job_id = _held_copy(unit, drives)
    mountpoint = drives.base / "STICK"
    refused = [
        ("POST", "/system/storage/mount", {"device": "/dev/sda1"}),
        ("DELETE", "/system/storage/mount", {"mountpoint": str(mountpoint)}),
        ("POST", "/system/storage/activate", {"path": str(mountpoint)}),
        ("POST", "/system/storage/activate", {"path": str(mountpoint / "sub")}),
    ]
    (mountpoint / "sub").mkdir()
    for method, path, body in refused:
        resp = _admin_storage(unit, method, path, body)
        _code(resp, 409, "usb_copy_running")
    # Another drive whose administrator's mountpoint name is the copy's.
    storage_routes["partitions"][1]["label"] = "STICK"
    _code(_admin_storage(unit, "POST", "/system/storage/mount", {"device": "/dev/sdb1"}), 409, "usb_copy_running")
    assert storage_routes["run"] == [] and storage_routes["override"] == []
    (mountpoint / "sub").rmdir()
    drives.gate.set()
    assert _wait(job_id).state == "done"
    # Once the copy has ended the same drive is the administrator's again.
    resp = _admin_storage(unit, "POST", "/system/storage/mount", {"device": "/dev/sda1"})
    assert resp.status_code == 200, resp.text
    assert storage_routes["run"] == [["sudo", HELPER, "mount", "/dev/sda1", str(mountpoint)]]


@pytest.mark.unit
def test_with_no_copy_the_administrator_mounts_activates_and_unmounts_as_before(unit, drives, storage_routes,
                                                                             tmp_path):
    from app.core.system_helper import HELPER
    mountpoint = drives.base / "STICK"
    resp = _admin_storage(unit, "POST", "/system/storage/mount", {"device": "/dev/sda1"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"mountpoint": str(mountpoint), "message": f"Mounted at {mountpoint}"}
    drive = tmp_path / "a-drive"
    drive.mkdir()
    resp = _admin_storage(unit, "POST", "/system/storage/activate", {"path": str(drive)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["projects_path"] == str(drive / "dtk-projects")
    assert storage_routes["override"] == [str(drive / "dtk-projects")]
    resp = _admin_storage(unit, "DELETE", "/system/storage/mount", {"mountpoint": str(mountpoint)})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"message": "Device unmounted successfully.", "override_cleared": False}
    assert storage_routes["run"] == [["sudo", HELPER, "mount", "/dev/sda1", str(mountpoint)],
                                     ["sudo", HELPER, "umount", str(mountpoint)]]
    # The routes' own refusals are as they were.
    assert _admin_storage(unit, "POST", "/system/storage/mount", {"device": "sda1"}).json() == {
        "detail": "Invalid device path."}
    assert _admin_storage(unit, "DELETE", "/system/storage/mount", {"mountpoint": "/etc"}).json() == {
        "detail": "Only paths Captua mounted can be unmounted."}


def _inside(path, *tops):
    real = os.path.realpath(path)
    return any(real == top.rstrip(os.sep) or real.startswith(top) for top in tops)


@pytest.mark.unit
def test_the_copy_touches_only_the_volumes_directory_and_the_mountpoint(unit, drives, fs):
    from app.core.config import settings
    unit.backend.raw = True
    _project, volume = _volume(unit, captures=2)
    assert _run(unit, volume["id"])["state"] == "done"
    assert _run(unit, volume["id"])["state"] == "done"
    directory = os.path.realpath(Path(settings.projects_dir) / volume["filename_stem"]) + os.sep
    mountpoint = os.path.normpath(drives.base / "STICK") + os.sep
    kinds = {call[0] for call in fs.calls}
    assert {"open", "lstat", "lexists", "mkdir", "rename"} <= kinds, kinds
    for call in fs.calls:
        for path in call[1:3]:
            if isinstance(path, str):
                assert _inside(path, mountpoint, directory), call
        if call[0] == "open" and _inside(call[1], directory):
            assert not call[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC), call
            assert call[2] & os.O_NOFOLLOW, call
        if call[0] == "open" and call[1].endswith(".part"):
            assert call[2] & os.O_EXCL and call[2] & os.O_CREAT, call
        if call[0] in ("rename", "replace", "unlink", "remove", "mkdir", "makedirs", "rmdir"):
            assert _inside(call[1], mountpoint), call
        if call[0] in ("unlink", "remove"):
            assert call[1].endswith(".part"), call
        if call[0] in ("rename", "replace"):
            assert call[1] == call[2] + ".part", call


@pytest.mark.unit
def test_a_row_naming_an_ordinary_volumes_file_is_not_opened(unit, drives, fs):
    _project, volume = _volume(unit, captures=1)
    ordinary = [Path(r.file_path) for r in _rows(unit, unit.real_volume.id)]
    assert ordinary and all(p.is_file() for p in ordinary)
    row = _rows(unit, volume["id"])[0]
    row.file_path = str(ordinary[0])
    unit.db.commit()
    status = _run(unit, volume["id"])
    assert status["state"] == "done" and status["skipped"]["outside"] == 1 and status["done"] == 1, status
    real_dir = os.path.realpath(ordinary[0].parent.parent) + os.sep
    assert not [p for p in _paths_touched(fs) if _inside(p, real_dir)]
    assert ordinary[0].read_bytes() not in _on_drive(drives).values()


@pytest.mark.unit
def test_a_row_naming_another_testing_volumes_file_is_not_opened(unit, drives, fs):
    project, volume = _volume(unit, captures=1)
    other = unit.volume(project_id=project["id"], name="Vecino")
    _capture(unit, project, other["id"])
    theirs = Path(_rows(unit, other["id"])[0].file_path)
    row = _rows(unit, volume["id"])[0]
    row.file_path = str(theirs)
    unit.db.commit()
    status = _run(unit, volume["id"])
    assert status["state"] == "done" and status["skipped"]["outside"] == 1, status
    their_dir = os.path.realpath(theirs.parent.parent) + os.sep
    assert not [p for p in _paths_touched(fs) if _inside(p, their_dir)]


@pytest.mark.unit
def test_a_captures_directory_symlinked_to_an_ordinary_directory_is_not_read(unit, drives, fs):
    from app.core.config import settings
    _project, volume = _volume(unit, captures=1)
    stem = volume["filename_stem"]
    captures = Path(settings.projects_dir) / stem / "captures"
    ordinary = Path(settings.projects_dir) / unit.real_volume.filename_stem / "captures"
    names = [Path(r.file_path).name for r in _rows(unit, volume["id"])]
    for name in names:
        (captures / name).rename(ordinary / name)
    captures.rmdir()
    captures.symlink_to(ordinary, target_is_directory=True)
    status = _run(unit, volume["id"])
    _failed(status, "usb_volume_empty")
    assert status["skipped"]["outside"] == 2, status
    assert not [c for c in fs.calls if c[0] in ("open", "lstat", "stat", "lexists", "exists")
                and _inside(c[1], os.path.realpath(ordinary) + os.sep)]
    assert drives.calls == []


# ---------------------------------------------------------------------------
# F. The system log
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_start_and_the_end_of_each_copy_are_logged(unit, drives):
    from app.models.system_log import SystemLog
    _project, volume = _volume(unit, captures=1)
    _run(unit, volume["id"])
    drives.fail.add("mount")
    _run(unit, volume["id"], caller="admin")
    unit.db.expire_all()
    rows = unit.db.query(SystemLog).filter(SystemLog.action.like("testing_usb_copy_%")).order_by(SystemLog.id).all()
    assert [(r.action, r.actor, r.category) for r in rows] == [
        ("testing_usb_copy_started", "u-testing", "system"), ("testing_usb_copy_ended", "u-testing", "system"),
        ("testing_usb_copy_started", "u-admin", "system"), ("testing_usb_copy_ended", "u-admin", "system"),
    ]
    stem = volume["filename_stem"]
    for row in rows:
        assert stem in row.detail and "STICK" in row.detail and "sda1" in row.detail, row.detail
        assert row.subject == stem
    assert "result done" in rows[1].detail and rows[1].level == "INFO"
    assert "result usb_mount" in rows[3].detail and rows[3].level == "WARN"
