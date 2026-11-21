"""The testing account's copy of a testing volume's pictures to a USB drive (NEH-391).

What holds:

* Which drives are offered: eligible_drives(), and nothing else. A
  partition is offered only when all of these hold, and nothing else is
  ever offered:
  1. its disk's transport is USB;
  2. every node of that disk's lsblk tree other than the disk itself is of
     TYPE `part`, and no partition has children: a disk with any other
     node at any depth (`crypt`, `lvm`, `raid*`, `mpath`, `loop`, a
     partition with children) is not offered at all;
  3. no node of that disk, the disk node or any partition, has a
     mountpoint in lsblk's column (`[SWAP]` included) or in the kernel's
     mount table (MOUNT_TABLE, read since lsblk reports one mountpoint per
     device): a disk with anything of it in use anywhere is not offered at
     all;
  4. no partition of that disk is the one the storage override names (its
     mountpoint name, as the administrator's mount gives it, at or above
     the override root), mounted or not;
  5. the partition's own file system is vfat or exFAT and its name matches
     the helper's device pattern;
  6. lsblk, the mount table and the override file could all be read.
  vfat and exFAT hold no symbolic links and are mounted by the helper with
  the backend's uid and gid, so every file the copy creates belongs to the
  backend's user and no path on the drive leads elsewhere.
* The device a request names is matched against that list when the
  request arrives, and again when the job starts, where it must be the
  same drive (name, label, size and file system as at the request). A
  command receives only the matched entry's name and the mountpoint the
  job builds, never the request's string.
* One copy at a time on the unit (`usb_copy_busy`). For its whole run the
  job holds its volume with export_jobs.hold_for_copy, so a request that
  takes hold_for_file_changes on the volume is refused with a 409: a move
  of its records, a move to another project, a re-parent, a rename, a
  record's delete and an image's delete. The copy reads the images current
  when it starts; an image's delete takes the hold while a copy holds its
  volume whether the image is current or not, so an image a retake
  supersedes during the copy stays until the copy ends. A purge, a
  clear or the testing account's delete leaves the volume and counts it as
  skipped (app.core.testing_space.purge_volume). A volume a purge has
  marked takes no copy. The drive and its mountpoint are the copy's while
  it runs: the administrator's storage routes refuse them
  (`usb_copy_running`, UsbCopyRegistry.holds_drive).
* What is read: the files the volume's current image rows name, never a
  listing of a directory. For each row, its master; for a master that is
  not RAW, its `.raw` raw buffer when present (app.core.paths.raw_sidecar_of),
  probed only once its path has passed the check below; and, when at least
  one current master's suffix is neither `.jpg` nor `.jpeg` (casefolded),
  each RAW master's `_preview.jpg` (app.core.paths.preview_sidecar_of).
  Previews are made only for RAW masters, so a volume of JPEG masters has
  none to copy. Each file must lie at `<projects root>/<the volume's
  stem>/captures/<name>` or `.../previews/<name>` and pass
  testing_space._testing_place (inside a testing directory of a readable
  known root, no symlink below the root); anything else is counted
  `outside` and never opened or probed. A file whose lstat is not a regular
  file is counted `not_regular`, and so is one that opens as something
  else; one absent at lstat or at open is counted `missing`. Any other
  error opening or reading a source fails the job (`usb_read`). Sources are
  opened read-only with O_NOFOLLOW. Nothing on the unit is written,
  renamed or deleted.
* The names: the stored ones. A testing volume's stem is the testing
  prefix and six or more digits (testing_space.series_stem); a capture's
  names are `<stem>-img_<nnnn>_c<n><suffix>` (app.core.capture_naming)
  with the suffix the capture backend writes (`.jpg`, `.cr2`, `.dng` and
  the like), an upload's suffix is one of app.api.records._UPLOAD_SUFFIXES,
  a preview ends in `_preview.jpg` and a raw buffer in `.raw`. So the
  characters are ASCII letters, digits, `-`, `_` and `.`, none of which
  vfat or exFAT forbids. Every name is checked against FAT_NAME before any
  file is opened, and two names equal once casefolded are refused too,
  since both file systems treat them as one name: `usb_name`, never a
  rename.
* What is written: one new folder per copy, `<stem>_<YYYYMMDD-HHMMSS>` in
  UTC (_now, read when the copy is asked for), created with an exclusive
  mkdir, with `captures/` and `previews/` in it as needed; a folder of
  that name already on the drive fails the job (`usb_exists`). Its real
  path must lie inside the mountpoint's. Each file is written to
  `<name>.part`, created exclusively (O_EXCL), flushed with fsync and
  renamed to its name; a `.part` or a final name that already exists in
  the new folder fails the job (`usb_write`). Before writing, the free
  space must hold every file rounded up to the drive's allocation unit
  (_allocation_unit) plus FREE_MARGIN_BYTES (`usb_full`); ENOSPC or
  EDQUOT while writing is `usb_full` too, any other write error
  `usb_write`. The only file the job ever unlinks is the `.part` it
  created and is writing when it fails.
* The drive is always mounted by the job, at MOUNT_BASE/<its name as the
  administrator's mount names it>. Once the helper has mounted it, the
  rule is applied again with one exception, that the job's partition has
  exactly the job's mountpoint (every other node of its disk still
  unmounted, the tree still all `part`), and the device must carry the
  label, size and file system it had at the request; otherwise nothing is
  written and the job fails `usb_not_eligible`. Once the helper has mounted it the job attempts
  exactly one unmount, after fsyncing its folders and calling
  os.sync(). Before that unmount the storage override is read again: when
  it lies at or below the mountpoint, or cannot be read, the drive is left
  mounted and the job reports `usb_unmount`, as it does for an unmount
  that fails. It is never retried.
* Restart: jobs live in memory, as exports do. A copy cut by a restart
  leaves its folder on the drive, with `.part` files in it, and the drive
  mounted. Nothing unmounts a drive at start: the backend cannot know who
  mounted it. A mounted drive is not offered, so it takes a copy again
  once it has been unmounted (DELETE /system/storage/mount) or plugged in
  again; that copy writes a new folder and leaves the cut one as it is.

The helper is reached through _run_helper only, as `sudo
/usr/local/bin/dtk-system-helper mount <device> <mountpoint>` and `...
umount <mountpoint>`, the verbs and arguments the installed helper takes;
lsblk through _run_lsblk only; the free space, the allocation unit, the
clock and the job's thread through _free_bytes, _allocation_unit, _now and
_start_thread.

Version: 0.3.3
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# The directory the helper mounts drives under, one level deep.
MOUNT_BASE = Path("/var/lib/dtk/mounts")
# The free space a copy leaves on the drive beyond the bytes it writes.
FREE_MARGIN_BYTES = 64 * 1024 * 1024
# The bytes read and written at a time.
CHUNK = 1024 * 1024
LSBLK_TIMEOUT_SECONDS = 10
HELPER_TIMEOUT_SECONDS = 30
# How many finished jobs the registry keeps for their status.
KEEP_FINISHED = 20

FILE_SYSTEMS = frozenset({"vfat", "exfat"})
# The helper's device pattern (dtk-system-helper DEVICE_RE).
DEVICE_RE = re.compile(r"^/dev/(sd[a-z][0-9]+|mmcblk[0-9]+p[0-9]+|nvme[0-9]+n[0-9]+p[0-9]+)$")
# A name vfat and exFAT both hold: ASCII letters, digits, `-`, `_`, `.`, 1 to 255 characters, not
# beginning or ending with a dot.
FAT_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,253}[A-Za-z0-9_-]$|^[A-Za-z0-9_-]$")
_JPEG_SUFFIXES = frozenset({".jpg", ".jpeg"})
_SUBDIRS = ("captures", "previews")
_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})

# Failure codes.
USB_NOT_ELIGIBLE = "usb_not_eligible"
USB_COPY_BUSY = "usb_copy_busy"
USB_COPY_RUNNING = "usb_copy_running"
USB_MOUNT = "usb_mount"
USB_FULL = "usb_full"
USB_WRITE = "usb_write"
USB_READ = "usb_read"
USB_NAME = "usb_name"
USB_EXISTS = "usb_exists"
USB_UNMOUNT = "usb_unmount"
USB_VOLUME_EMPTY = "usb_volume_empty"
USB_REAL_VOLUME = "usb_real_volume"

MESSAGES = {
    USB_NOT_ELIGIBLE: "This drive cannot take a copy. Use a USB drive formatted FAT32 or exFAT.",
    USB_COPY_BUSY: "Another copy to a USB drive is running. Try again once it has finished.",
    USB_COPY_RUNNING: "A testing volume is being copied to this drive. Try again once the copy has finished.",
    USB_MOUNT: "The drive could not be opened. Unplug it, plug it in again and try once more.",
    USB_FULL: "The drive does not have enough free space for this volume.",
    USB_WRITE: "Writing to the drive failed. The drive may have been unplugged or be faulty.",
    USB_READ: "A picture of this volume could not be read on the unit.",
    USB_NAME: "A file name of this volume cannot be used on the drive.",
    USB_EXISTS: "The drive already holds a folder of this name. Try again in a moment.",
    USB_UNMOUNT: "The copy could not close the drive. Do not unplug it; ask an administrator to unmount it.",
    USB_VOLUME_EMPTY: "This volume has no pictures to copy.",
    USB_REAL_VOLUME: "Only testing volumes are copied to a USB drive.",
}


class UsbRefused(Exception):
    """A refusal with an HTTP status and a {code, message} detail."""

    def __init__(self, status: int, code: str, message: Optional[str] = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or MESSAGES[code]

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message}


class _Failed(Exception):
    """A job's failure code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# The system commands, the drive's measures, the clock and the thread, each behind one function
# ---------------------------------------------------------------------------

def _run_lsblk() -> str:
    """lsblk's JSON tree, sizes in bytes, with each device's transport and parent. Raises on any failure."""
    result = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL,RM,TYPE,TRAN,PKNAME"],
        capture_output=True, text=True, timeout=LSBLK_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise OSError(f"lsblk exited {result.returncode}: {(result.stderr or '').strip()}")
    return result.stdout


def _run_helper(args: list) -> tuple:
    """`sudo HELPER <args>`, stdin closed; (True, "") on exit 0, else (False, its message)."""
    from app.core.system_helper import HELPER

    try:
        result = subprocess.run(["sudo", HELPER, *args], capture_output=True, text=True,
                                timeout=HELPER_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
    return True, ""


# The kernel's table of every mount, read beside lsblk, which reports one mountpoint per device.
MOUNT_TABLE = "/proc/self/mounts"


def _read_mount_table() -> str:
    """The kernel's mount table as text. Raises OSError when it cannot be read."""
    with open(MOUNT_TABLE, encoding="utf-8", errors="replace") as f:
        return f.read()


def _unescape(field: str) -> str:
    """A mount table field with its octal escapes (`\\040` for a space) decoded."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _mount_table_by_device(text: str) -> dict:
    """{device name: its mountpoints} for every `/dev/<name>` line of a mount table."""
    found = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2 or not fields[0].startswith("/dev/"):
            continue
        found.setdefault(os.path.basename(fields[0]), set()).add(os.path.normpath(_unescape(fields[1])))
    return found


def _free_bytes(path) -> int:
    import shutil

    return shutil.disk_usage(path).free


def _allocation_unit(path) -> int:
    """The file system's allocation unit at `path`: statvfs's fragment size, or its block size."""
    vfs = os.statvfs(path)
    return int(vfs.f_frsize or vfs.f_bsize or 1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _start_thread(target, args) -> None:
    threading.Thread(target=target, args=args, name="usb-copy", daemon=True).start()


# ---------------------------------------------------------------------------
# Which drives are offered
# ---------------------------------------------------------------------------

def mount_name(label: Optional[str], name: str) -> str:
    """The mountpoint name POST /system/storage/mount gives a drive: its label or name, [A-Za-z0-9_-], 32 characters."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", label or name)[:32]


def _at_or_below(path: str, top: str) -> bool:
    path, top = os.path.normpath(path), os.path.normpath(top)
    return path == top or path.startswith(top.rstrip(os.sep) + os.sep)


def _size(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class _Disk:
    """One disk as lsblk's tree gives it, whole, with every mountpoint of its nodes from lsblk and the mount table."""

    def __init__(self, node: dict, table: dict):
        self.node = node
        self.partitions = [c for c in node.get("children") or [] if isinstance(c, dict)]
        # Every node below the disk at any depth, for the all-`part` test.
        self.descendants, stack = [], list(self.partitions)
        while stack:
            current = stack.pop()
            self.descendants.append(current)
            stack.extend(c for c in current.get("children") or [] if isinstance(c, dict))
        self.mounts = {}
        for current in [node] + self.descendants:
            name = current.get("name")
            points = set(table.get(name, ())) if isinstance(name, str) else set()
            if current.get("mountpoint"):
                points.add(current["mountpoint"] if current["mountpoint"] == "[SWAP]"
                           else os.path.normpath(current["mountpoint"]))
            self.mounts[name] = points

    def all_part(self) -> bool:
        """Rule 2: every node below the disk is a partition, and no partition has children."""
        return all(d.get("type") == "part" and not d.get("children") for d in self.descendants)

    def in_use(self, allowed: Optional[dict] = None) -> bool:
        """Rule 3: whether any node of the disk has a mountpoint; `allowed` gives one node its expected set."""
        allowed = allowed or {}
        for name, points in self.mounts.items():
            if name in allowed:
                if points != allowed[name]:
                    return True
            elif points:
                return True
        return False


def _disks() -> Optional[tuple]:
    """(the disks lsblk lists, as _Disk, the storage override); None when lsblk, the mount table or the
    override file cannot be read (rule 6)."""
    from app.core import storage_override

    try:
        tree = json.loads(_run_lsblk())
    except Exception as exc:
        logger.warning("lsblk could not be read for the USB copy: %s", exc)
        return None
    try:
        table = _mount_table_by_device(_read_mount_table())
    except Exception as exc:
        logger.warning("The mount table could not be read for the USB copy: %s", exc)
        return None
    override, corrupt = storage_override.get_storage_override_or_fallback()
    if corrupt:
        return None
    disks = [_Disk(node, table) for node in tree.get("blockdevices") or []
             if isinstance(node, dict) and node.get("type") == "disk"]
    return disks, override


def _named_by_override(part: dict, override: Optional[str]) -> bool:
    """Whether the override names this partition: its mountpoint name, as the administrator's mount gives
    it, is at or above the override root."""
    if not override:
        return False
    named_at = os.path.join(os.path.normpath(str(MOUNT_BASE)), mount_name(part.get("label"), part.get("name") or ""))
    return _at_or_below(override, named_at) or _at_or_below(os.path.realpath(override), named_at)


def _disk_may_take_a_copy(disk: _Disk, override: Optional[str], allowed: Optional[dict] = None) -> bool:
    """Rules 1 to 4 for a whole disk."""
    return (disk.node.get("tran") == "usb" and disk.all_part() and not disk.in_use(allowed)
            and not any(_named_by_override(p, override) for p in disk.partitions))


def _fat(part: dict) -> bool:
    """Rule 5."""
    return part.get("fstype") in FILE_SYSTEMS and bool(DEVICE_RE.match(f"/dev/{part.get('name')}"))


def _entry(part: dict) -> dict:
    return {"name": part["name"], "label": part.get("label") or None, "size": _size(part.get("size")),
            "fstype": part.get("fstype"), "free_bytes": None}


def eligible_drives() -> list:
    """Every partition a copy may go to, as {name, label, size, fstype, free_bytes}: the rule in the module docstring.

    `size` is in bytes. `free_bytes` is always None: an offered drive is not
    mounted, so its free space cannot be read.
    """
    read = _disks()
    if read is None:
        return []
    disks, override = read
    offered = []
    for disk in disks:
        if not _disk_may_take_a_copy(disk, override):
            continue
        offered += [_entry(p) for p in disk.partitions if _fat(p)]
    return sorted(offered, key=lambda d: d["name"])


def _mounted_as_the_job(job) -> bool:
    """The rule again once the helper has mounted, with one exception: the job's partition has exactly the
    job's mountpoint. It must also carry the label, size and file system it had at the request."""
    read = _disks()
    if read is None:
        return False
    disks, override = read
    for disk in disks:
        part = next((p for p in disk.partitions if p.get("name") == job.device), None)
        if part is None:
            continue
        return (_disk_may_take_a_copy(disk, override, allowed={job.device: {job.mountpoint}})
                and _fat(part) and _same_drive(job.drive, _entry(part)))
    return False


def _find(device: str) -> Optional[dict]:
    if not isinstance(device, str):
        return None
    return next((d for d in eligible_drives() if d["name"] == device), None)


def _same_drive(a: dict, b: Optional[dict]) -> bool:
    return b is not None and all(a.get(k) == b.get(k) for k in ("name", "label", "size", "fstype"))


# ---------------------------------------------------------------------------
# What is copied
# ---------------------------------------------------------------------------

def _named(db, volume) -> list:
    """The masters of the volume's current image rows and, with previews, each RAW master's preview: [(path, is master)]."""
    from app.core.paths import is_raw_suffix, preview_sidecar_of
    from app.models.record import Record, RecordImage

    rows = (db.query(RecordImage.file_path)
            .join(Record, RecordImage.record_id == Record.id)
            .filter(Record.collection_id == volume.id, RecordImage.is_current.is_(True))
            .order_by(RecordImage.id).all())
    masters = [Path(fp) for (fp,) in rows if fp]
    with_previews = any(m.suffix.lower() not in _JPEG_SUFFIXES for m in masters)
    named = []
    for master in masters:
        named.append((master, True))
        if is_raw_suffix(master) and with_previews:
            named.append((preview_sidecar_of(master), False))
    return named


def _check_names(paths) -> None:
    """_Failed(USB_NAME) for a name FAT_NAME refuses, or two names in one folder equal once casefolded."""
    names = {}
    for path in paths:
        if not FAT_NAME.match(path.name):
            raise _Failed(USB_NAME)
        folded = (path.parent.name, path.name.casefold())
        if folded in names and names[folded] != path.name:
            raise _Failed(USB_NAME)
        names[folded] = path.name


def _placed(path, roots, stem):
    """(normalised path, path relative to `<stem>/`) when the path passes the check; None otherwise."""
    from app.core.testing_space import _testing_place

    norm, directory, refusal = _testing_place(path, roots)
    if refusal is not None or directory is None or os.path.basename(directory) != stem:
        return None
    rel = os.path.relpath(norm, directory)
    parts = rel.split(os.sep)
    if len(parts) != 2 or parts[0] not in _SUBDIRS:
        return None
    return norm, rel


def _sources(db, volume) -> tuple:
    """(files, skipped): [(source path, path relative to `<stem>/`, size)] to copy, and the counts skipped.

    _Failed(USB_NAME) for a name the drive cannot hold, checked on the
    stored names before any file is looked at.
    """
    from app.core.paths import is_raw_suffix, raw_sidecar_of
    from app.core.testing_space import _known_roots

    unique, seen = [], set()
    for path, is_master in _named(db, volume):
        key = os.path.normpath(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            unique.append((Path(key), is_master))
    _check_names(p for p, _m in unique)

    roots = _known_roots()
    skipped = {"not_regular": 0, "outside": 0, "missing": 0}
    candidates = []
    for path, is_master in unique:
        placed = _placed(path, roots, volume.filename_stem)
        if placed is None:
            skipped["outside"] += 1
            continue
        candidates.append(placed)
        if is_master and not is_raw_suffix(path):
            buffer = raw_sidecar_of(path)
            key = os.path.normpath(os.path.abspath(str(buffer)))
            buffer_placed = _placed(buffer, roots, volume.filename_stem)
            if buffer_placed is not None and key not in seen and os.path.lexists(buffer_placed[0]):
                seen.add(key)
                candidates.append(buffer_placed)
    _check_names(Path(norm) for norm, _rel in candidates)
    files = []
    for norm, rel in candidates:
        try:
            st = os.lstat(norm)
        except FileNotFoundError:
            skipped["missing"] += 1
            continue
        except OSError:
            raise _Failed(USB_READ)
        if not stat.S_ISREG(st.st_mode):
            skipped["not_regular"] += 1
            continue
        files.append((norm, rel, st.st_size))
    return files, skipped


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

class UsbCopyJob:
    def __init__(self, collection_id: int, stem: str, drive: dict, mountpoint: str, folder: str, actor: str):
        self.id = uuid.uuid4().hex
        self.collection_id = collection_id
        self.stem = stem
        self.drive = dict(drive)
        self.device = drive["name"]
        self.label = drive.get("label")
        self.mountpoint = mountpoint
        self.folder = folder
        self.actor = actor
        self.state = "queued"
        self.done = 0
        self.total = 0
        self.bytes_done = 0
        self.bytes_total = 0
        self.error: Optional[str] = None
        self.skipped = {"not_regular": 0, "outside": 0, "missing": 0}
        self.unmounted = False
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.finished = threading.Event()

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "collection_id": self.collection_id,
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "error": self.error,
            "message": MESSAGES.get(self.error) if self.error else None,
            "folder": self.folder,
            "drive": self.label or self.device,
            "skipped": dict(self.skipped),
            "unmounted": self.unmounted,
        }


def _log(db, job: UsbCopyJob, action: str, result: Optional[str], level: str = "INFO") -> None:
    from app.core.audit import log_event

    detail = (f"volume {job.collection_id} ({job.stem}); drive {job.label or '-'} ({job.device})"
              + (f"; result {result}; files {job.done}/{job.total}" if result is not None else ""))
    log_event(db, level=level, category="system", action=action, actor=job.actor, subject=job.stem, detail=detail)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _inside(path: str, top: str) -> bool:
    real, top = os.path.realpath(path), os.path.realpath(top)
    return real.startswith(top.rstrip(os.sep) + os.sep)


def _write_error(exc: OSError) -> _Failed:
    return _Failed(USB_FULL if exc.errno in _FULL_ERRNOS else USB_WRITE)


def _make_folders(mountpoint: str, folder: str, subdirs) -> list:
    """The new folder and its subfolders, each made with an exclusive mkdir; _Failed(USB_EXISTS) when the folder is there."""
    made = []
    for path in [folder] + [os.path.join(folder, s) for s in subdirs]:
        try:
            os.mkdir(path)
        except FileExistsError:
            raise _Failed(USB_EXISTS if path == folder else USB_WRITE)
        except OSError as exc:
            raise _write_error(exc)
        made.append(path)
        if not _inside(path, mountpoint):
            raise _Failed(USB_WRITE)
    return made


def _copy_one(job: UsbCopyJob, src: str, dest: str) -> None:
    """src to `<dest>.part`, created exclusively and fsynced, then renamed to dest; that `.part` unlinked on failure."""
    try:
        in_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        job.skipped["missing"] += 1
        return
    except OSError:
        raise _Failed(USB_READ)
    part = dest + ".part"
    out_fd = None
    created = False
    try:
        if not stat.S_ISREG(os.fstat(in_fd).st_mode):
            job.skipped["not_regular"] += 1
            return
        try:
            out_fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            raise _Failed(USB_WRITE)
        except OSError as exc:
            raise _write_error(exc)
        created = True
        while True:
            try:
                chunk = os.read(in_fd, CHUNK)
            except OSError:
                raise _Failed(USB_READ)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                try:
                    written = os.write(out_fd, view)
                except OSError as exc:
                    raise _write_error(exc)
                view = view[written:]
                job.bytes_done += written
        try:
            os.fsync(out_fd)
            os.close(out_fd)
            out_fd = None
        except OSError as exc:
            raise _write_error(exc)
        if os.path.lexists(dest):
            raise _Failed(USB_WRITE)
        try:
            os.rename(part, dest)
        except OSError as exc:
            raise _write_error(exc)
        job.done += 1
    except _Failed:
        if out_fd is not None:
            try:
                os.close(out_fd)
            except OSError:
                pass
        if created:
            try:
                os.unlink(part)
            except OSError:
                pass
        raise
    finally:
        os.close(in_fd)


def _rounded(size: int, unit: int) -> int:
    return -(-size // unit) * unit if unit > 0 else size


def _override_on(mountpoint: str) -> bool:
    """Whether the storage override lies at or below the mountpoint, or cannot be read."""
    from app.core import storage_override

    override, corrupt = storage_override.get_storage_override_or_fallback()
    if corrupt:
        return True
    return bool(override) and (_at_or_below(override, mountpoint)
                               or _at_or_below(os.path.realpath(override), os.path.realpath(mountpoint)))


def _work(job: UsbCopyJob) -> None:
    """The copy, on its own session; sets job.error."""
    from app.core.db import SessionLocal
    from app.models.collection import Collection

    mounted = False
    folders = []
    db = SessionLocal()
    try:
        try:
            if not _same_drive(job.drive, _find(job.device)):
                raise _Failed(USB_NOT_ELIGIBLE)
            volume = db.get(Collection, job.collection_id)
            if volume is None:
                raise _Failed(USB_VOLUME_EMPTY)
            files, skipped = _sources(db, volume)
            job.skipped.update(skipped)
            db.rollback()
            if not files:
                raise _Failed(USB_VOLUME_EMPTY)
            job.total = len(files)
            job.bytes_total = sum(size for _s, _r, size in files)

            mountpoint = job.mountpoint
            if os.path.lexists(mountpoint):
                raise _Failed(USB_MOUNT)
            ok, message = _run_helper(["mount", f"/dev/{job.device}", mountpoint])
            if not ok:
                logger.error("Mounting %s for a USB copy failed: %s", job.device, message)
                raise _Failed(USB_MOUNT)
            mounted = True
            if not _mounted_as_the_job(job):
                raise _Failed(USB_NOT_ELIGIBLE)
            folder = os.path.join(mountpoint, job.folder)
            if not _inside(folder, mountpoint):
                raise _Failed(USB_WRITE)
            try:
                unit = int(_allocation_unit(mountpoint))
                free = int(_free_bytes(mountpoint))
            except OSError:
                raise _Failed(USB_WRITE)
            needed = sum(_rounded(size, unit) for _s, _r, size in files)
            if free < needed + FREE_MARGIN_BYTES:
                raise _Failed(USB_FULL)
            subdirs = sorted({rel.split(os.sep)[0] for _s, rel, _z in files})
            folders = _make_folders(mountpoint, folder, subdirs)
            for src, rel, _size in files:
                _copy_one(job, src, os.path.join(folder, rel))
            if job.done == 0:
                raise _Failed(USB_VOLUME_EMPTY)
        except _Failed as failure:
            job.error = failure.code
        except Exception:
            logger.exception("USB copy %s of volume %s failed", job.id, job.collection_id)
            job.error = USB_WRITE
        # The end, on success and on failure.
        try:
            for path in reversed(folders):
                _fsync_dir(path)
        except OSError:
            job.error = job.error or USB_WRITE
        os.sync()
        if mounted:
            if _override_on(job.mountpoint):
                logger.error("The storage override lies on %s; the drive is left mounted", job.mountpoint)
                job.error = job.error or USB_UNMOUNT
            else:
                ok, message = _run_helper(["umount", job.mountpoint])
                if ok:
                    job.unmounted = True
                else:
                    logger.error("Unmounting %s after a USB copy failed: %s", job.mountpoint, message)
                    job.error = job.error or USB_UNMOUNT
        try:
            _log(db, job, "testing_usb_copy_ended", job.error or "done", level="WARN" if job.error else "INFO")
        except Exception:
            logger.exception("The end of USB copy %s could not be logged", job.id)
    finally:
        db.close()


class UsbCopyRegistry:
    """The unit's USB copies, in memory: at most one queued or running, and the KEEP_FINISHED most recent finished."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._finished: deque = deque()
        self._active: Optional[str] = None

    def get(self, job_id: str) -> Optional[UsbCopyJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def running(self) -> Optional[UsbCopyJob]:
        """The job that holds the unit's one slot now (queued or running), or None."""
        with self._lock:
            job = self._jobs.get(self._active) if self._active else None
            return job if job is not None and job.state in ("queued", "running") else None

    def active(self) -> Optional[dict]:
        """{collection_id, job_id} of the job that holds the slot now, or None."""
        job = self.running()
        return None if job is None else {"collection_id": job.collection_id, "job_id": job.id}

    def holds_drive(self, *, device: Optional[str] = None, path=None) -> bool:
        """Whether a running copy holds this device (`/dev/<name>`), or a path at or below its mountpoint."""
        job = self.running()
        if job is None:
            return False
        if device is not None and device == f"/dev/{job.device}":
            return True
        if path is not None:
            raw = os.path.normpath(os.path.abspath(str(path)))
            return _at_or_below(raw, job.mountpoint) or _at_or_below(
                os.path.realpath(raw), os.path.realpath(job.mountpoint))
        return False

    def start(self, db, volume, device: str, actor: str, work: Callable[[UsbCopyJob], None] = None) -> UsbCopyJob:
        """Check the request and start the copy in a thread; UsbRefused for every refusal.

        In order: a real volume (409 usb_real_volume), another copy (409
        usb_copy_busy), a device not offered (422 usb_not_eligible), the volume
        held for file changes (409 testing_volume_busy), a volume a purge
        has marked (404 not_found). A thread that cannot be started releases
        the hold and the slot, and its error is raised. The start is logged.
        """
        from fastapi import HTTPException
        from app.core import testing_space
        from app.core.export_jobs import FilesChanging, export_jobs
        from app.models.collection import Collection

        if not volume.testing:
            raise UsbRefused(409, USB_REAL_VOLUME)
        # Checked again under the lock below; first here, since a running copy's drive is mounted
        # and so no longer offered.
        if self.running() is not None:
            raise UsbRefused(409, USB_COPY_BUSY)
        drive = _find(device)
        if drive is None:
            raise UsbRefused(422, USB_NOT_ELIGIBLE)
        with self._lock:
            active = self._jobs.get(self._active) if self._active else None
            if active is not None and active.state in ("queued", "running"):
                raise UsbRefused(409, USB_COPY_BUSY)
            hold = export_jobs.hold_for_copy([volume.id])
            try:
                hold.__enter__()
            except FilesChanging as exc:
                raise UsbRefused(409, testing_space.TESTING_VOLUME_BUSY, str(exc)) from exc
            try:
                row = db.query(Collection).filter(Collection.id == volume.id).populate_existing().first()
                if row is None:
                    raise HTTPException(status_code=404, detail=testing_space.not_found_detail())
                testing_space.refuse_volume_being_removed(row)
                testing_space.refuse_real_work(row)
            except Exception:
                hold.__exit__(None, None, None)
                raise
            mountpoint = os.path.join(os.path.normpath(str(MOUNT_BASE)), mount_name(drive["label"], drive["name"]))
            folder = f"{row.filename_stem}_{_now().astimezone(timezone.utc):%Y%m%d-%H%M%S}"
            job = UsbCopyJob(volume.id, row.filename_stem, drive, mountpoint, folder, actor)
            self._jobs[job.id] = job
            self._active = job.id
        try:
            _start_thread(self._run, (job, work or _work, hold))
        except BaseException:
            hold.__exit__(None, None, None)
            with self._lock:
                self._jobs.pop(job.id, None)
                if self._active == job.id:
                    self._active = None
            raise
        try:
            _log(db, job, "testing_usb_copy_started", None)
        except Exception:
            logger.exception("The start of USB copy %s could not be logged", job.id)
        return job

    def _run(self, job: UsbCopyJob, work, hold) -> None:
        job.state = "running"
        try:
            work(job)
        except Exception:
            logger.exception("USB copy %s failed", job.id)
            job.error = job.error or USB_WRITE
        finally:
            job.state = "failed" if job.error else "done"
            job.finished_at = time.time()
            hold.__exit__(None, None, None)
            with self._lock:
                if self._active == job.id:
                    self._active = None
                self._finished.append(job.id)
                while len(self._finished) > KEEP_FINISHED:
                    self._jobs.pop(self._finished.popleft(), None)
            job.finished.set()

    def wait(self, job_id: str, timeout: float) -> Optional[UsbCopyJob]:
        """The job once it has ended, or as it is after `timeout` seconds; None for an unknown id."""
        job = self.get(job_id)
        if job is not None:
            job.finished.wait(timeout)
        return job

    def wait_all(self, timeout: float) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.finished.wait(timeout)


# Module-level singleton: one backend process.
usb_copies = UsbCopyRegistry()
