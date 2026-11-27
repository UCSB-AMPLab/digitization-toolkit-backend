"""
Each camera's recent libgphoto2 log lines, kept in memory and written to a
file when a capture fails or times out.

Deployed units have no USB recorder, so these lines are the only record of
what libgphoto2 and the body said to each other just before a failure.

Attribution
-----------
libgphoto2 has one log callback for the whole process (gp_log_add_func).
It calls the callback synchronously, on the thread making the libgphoto2
call, so a line belongs to the camera whose lock that thread holds: the
gphoto2 backend's per-camera lock binds the thread to its position on
acquire and unbinds it on release (bind / unbind). A line from a thread
bound to no camera is dropped, because it belongs to no one camera, and so
is a line from inside unattributed(): the backend wraps every bus-wide scan
in it (Camera.autodetect, CameraAbilitiesList.load, PortInfoList.load),
whose lines are about every port, even on a thread bound to one camera.
Every session close (_PTPSession.close) runs inside it too: by then
the position may already be published for the body that replaced the
session's, and a retired body's lines must not enter that buffer. The last
reference to a native camera is dropped inside it as well (the session's
close, and each brief identification camera before the next position's
lock is taken), because the binding's destructor (gp_camera_unref) logs.

Lines are kept per position, under one rule stated over CameraLogLines.

Bounds
------
LINES_PER_CAMERA lines per position, each cut to LINE_MAX_CHARS
characters. The callback is registered at GP_LOG_DEBUG, which excludes
GP_LOG_DATA, libgphoto2's hex dumps of PTP payloads. A failure file is
written under DIR_NAME in the backend's log directory, mode FILE_MODE, and
the directory keeps at most MAX_FILES failure files and MAX_TOTAL_BYTES in
all, the oldest deleted first. Files there that are not failure files are
never touched.

A failure file is written completely or not at all: every byte is written,
and on any error the partial file is removed. The cap runs whether the write
succeeded or not. Writing never raises: an error is logged as one warning
line and the capture's own failure goes on as it would have.

Version: 0.4.0
"""

import contextlib
import datetime
import errno
import os
import re
import threading
from collections import deque
from pathlib import Path

LINES_PER_CAMERA = 2000
LINE_MAX_CHARS = 256
MAX_FILES = 20
MAX_TOTAL_BYTES = 16 * 1024 * 1024
DIR_NAME = "camera-logs"
FILE_PREFIX = "capture-failure-"
FILE_MODE = 0o640
DIR_MODE = 0o750

_os_write = os.write

# Per thread: how many unattributed() blocks the thread is inside.
_SUSPENDED = threading.local()


@contextlib.contextmanager
def unattributed():
    """Keep the calling thread's libgphoto2 lines for no camera while inside."""
    _SUSPENDED.depth = getattr(_SUSPENDED, "depth", 0) + 1
    try:
        yield
    finally:
        _SUSPENDED.depth -= 1

_LEVEL_NAMES = {0: "ERROR", 1: "VERBOSE", 2: "DEBUG", 3: "DATA"}


def _short_body(body) -> str:
    """The last six letters and digits of a body's serial, or "unknown"."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(body or ""))
    return cleaned[-6:] if cleaned else "unknown"


class CameraLogLines:
    """Bounded per-position buffers of libgphoto2 log lines. Thread-safe.

    Lines are kept per position only. A position's buffer holds the lines
    since the body last recorded at that position took it, and it is cleared
    only when a different body occupies the position (occupy: a session
    cached, a port map published, an identification result). A closed
    session, or a position left out of a published map, keeps its lines and
    its occupancy record, so a failure written after either still has them;
    they go when the next different body takes the position. A body is known
    by its serial where one is known, else by its port: the same serial is
    the same body on any port, and the same port is the same body while
    either side's serial is unknown.

    Known limit: a native camera destroyed by the garbage collector logs into
    whichever position's buffer the collecting thread is bound to. These
    lines are diagnostics only. The backend releases every camera it holds
    explicitly, inside unattributed() (session close, brief identification
    cameras, failed construction and init), which keeps this to reference
    cycles the code does not create.
    """

    def __init__(self, lines=LINES_PER_CAMERA, line_chars=LINE_MAX_CHARS,
                 now=None):
        self._max_lines = lines
        self._line_chars = line_chars
        self._now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._lock = threading.Lock()
        self._buffers: dict = {}
        # position -> (port, serial or None) of the body now there.
        self._occupants: dict = {}
        self._bound = threading.local()
        self._handle = None
        self._installed_on = None

    # -- registration ------------------------------------------------------

    def install(self, gp_module) -> bool:
        """Register the libgphoto2 log callback once; False when unavailable.

        The handle python-gphoto2 returns is kept, because the callback is
        removed when it is collected.
        """
        if self._installed_on is gp_module and self._handle is not None:
            return True
        add = getattr(gp_module, "gp_log_add_func", None)
        level = getattr(gp_module, "GP_LOG_DEBUG", None)
        if add is None or level is None:
            return False
        result = add(level, self._callback)
        # python-gphoto2 returns (status, handle) or the handle alone.
        status, handle = 0, result
        if isinstance(result, (tuple, list)):
            status = result[0]
            handle = result[1] if len(result) == 2 else tuple(result[1:])
        elif isinstance(result, int):
            status, handle = result, None
        if status < 0:
            # Not registered: a later install() tries again.
            return False
        self._handle = handle
        self._installed_on = gp_module
        return True

    def _callback(self, level, domain, string, *data):
        try:
            self.record(level, domain, string)
        except Exception:
            # A callback that raises inside libgphoto2 has nowhere to go.
            pass

    # -- attribution -------------------------------------------------------

    def bind(self, position) -> None:
        stack = getattr(self._bound, "stack", None)
        if stack is None:
            stack = self._bound.stack = []
        stack.append(position)

    def unbind(self, position) -> None:
        stack = getattr(self._bound, "stack", None)
        if not stack:
            return
        for i in range(len(stack) - 1, -1, -1):
            if stack[i] == position:
                del stack[i]
                return

    # -- occupancy ---------------------------------------------------------

    @staticmethod
    def _same_body(current, port, serial) -> bool:
        current_port, current_serial = current
        if current_serial and serial:
            return current_serial == serial
        return current_port == port

    def occupy(self, position, port, serial) -> None:
        """The body on `port` (serial, or None) is at `position` now."""
        serial = serial or None
        with self._lock:
            current = self._occupants.get(position)
            if current is not None and not self._same_body(current, port, serial):
                self._buffers.pop(position, None)
                current = None
            if current is not None and serial is None:
                serial = current[1]
            self._occupants[position] = (port, serial)

    def move_port(self, old, new) -> None:
        """The body on `old` answers on `new` now; it stays the same body."""
        with self._lock:
            for position, (port, serial) in list(self._occupants.items()):
                if port == old:
                    self._occupants[position] = (new, serial)

    def occupant_serial(self, position):
        with self._lock:
            current = self._occupants.get(position)
        return current[1] if current else None

    # -- lines -------------------------------------------------------------

    def record(self, level, domain, text) -> None:
        stack = getattr(self._bound, "stack", None)
        if not stack or getattr(_SUSPENDED, "depth", 0):
            return
        position = stack[-1]
        stamp = self._now().strftime("%H:%M:%S.%f")[:-3]
        body_text = f"{domain}: {text}".rstrip("\n")[: self._line_chars]
        line = f"{stamp} {body_text}"
        if level not in (None, 2):
            line = f"{stamp} [{_LEVEL_NAMES.get(level, level)}] {body_text}"
        with self._lock:
            buf = self._buffers.get(position)
            if buf is None:
                buf = self._buffers[position] = deque(maxlen=self._max_lines)
            buf.append(line)

    def lines(self, position) -> list:
        with self._lock:
            return list(self._buffers.get(position, ()))

    # -- the failure file --------------------------------------------------

    def write_failure(self, log_dir, position, body, reason, logger,
                      max_files=MAX_FILES, max_bytes=MAX_TOTAL_BYTES):
        """Write `position`'s lines to a new file in log_dir; never raises.

        `body` is the identity known for the position at failure time, or
        None. Returns the file's path, or None when no complete file was
        written.
        """
        directory = Path(log_dir)
        path = None
        created = complete = False
        error = None
        try:
            directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            lines = self.lines(position)
            stamp = self._now().strftime("%Y%m%dT%H%M%S%f")[:-3]
            name = f"{FILE_PREFIX}{stamp}Z-camera{position}-{_short_body(body)}.log"
            path = directory / name
            header = (
                f"# camera position {position}, body {_short_body(body)}: {reason}\n"
                f"# last {len(lines)} libgphoto2 log lines\n"
            )
            data = (header + "".join(f"{line}\n" for line in lines)).encode(
                "utf-8", "replace"
            )
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            created = True
            try:
                os.fchmod(fd, FILE_MODE)
                view = memoryview(data)
                while view:
                    written = _os_write(fd, view)
                    if written <= 0:
                        raise OSError(errno.EIO, "write made no progress")
                    view = view[written:]
            finally:
                os.close(fd)
            complete = True
        except Exception as exc:
            error = exc
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
        finally:
            try:
                _enforce_cap(directory, path if complete else None,
                             max_files, max_bytes)
            except Exception as exc:
                error = error or exc
        if error is not None:
            logger.warning(
                f"[gphoto2] Camera {position}: the camera log lines were not "
                f"written in full ({error!r})"
            )
        return path if complete else None


def _enforce_cap(directory: Path, keep: Path, max_files: int, max_bytes: int) -> None:
    """Delete the oldest failure files until both caps hold; keep `keep`."""
    entries = []
    for path in directory.glob(f"{FILE_PREFIX}*.log"):
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        entries.append((st.st_mtime, path.name, path, st.st_size))
    entries.sort()
    total = sum(e[3] for e in entries)
    count = len(entries)
    for _mtime, _name, path, size in entries:
        if count <= max_files and total <= max_bytes:
            break
        if path == keep:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        count -= 1
        total -= size


# The one recorder of the process: libgphoto2's callback is process-wide.
RECORDER = CameraLogLines()
