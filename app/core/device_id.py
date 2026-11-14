"""The unit's device id (contract 1.11 §12.1): made once, at start-up, and kept.

The unit sends it with every request for a pairing code, and Fisqua shows
it to the approver beside the code. It is eight characters of lowercase
Crockford base-32 (ALPHABET, no i, l, o or u), drawn with `secrets`, which
matches the contract's `^[a-z0-9-]{1,32}$`. It is not a secret.

It lives in `device-id` in the data directory (CAPTUA_DATA_DIR,
/var/lib/dtk on a unit, where the other state files are), mode 0644.
The backend writes it once, at start-up beside unit_mode.initialise()
(app.main), and never again:

* it is written to a temporary file in the same directory, synced, and
  linked into place with os.link, which never replaces an existing file;
  a write that fails part way leaves no device-id, and of two writers the
  second keeps the first one's id;
* a file whose content is not a device id (one trailing newline is
  allowed, for a file written with an editor) is an error, never replaced:
  read() raises DeviceIdInvalid, state() answers `error: invalid`, and
  initialise() logs an ERROR and answers None. A file that cannot be read
  is `error: unreadable` in the same way. The caller (the pairing card)
  shows that the unit cannot ask for a code until someone repairs or
  removes the file; removing it makes a new id at the next start.

A copied SD card copies the id with it; nothing here detects that.

Version: 0.3.3
"""

import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Optional

# As a module: tests rebuild the Settings object.
from app.core import config

logger = logging.getLogger(__name__)

DEVICE_ID_FILE = "device-id"
ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
LENGTH = 8
PATTERN = re.compile(r"^[0-9a-hjkmnp-tv-z]{8}$")
FILE_MODE = 0o644


class DeviceIdInvalid(Exception):
    """The device-id file holds something that is not a device id. Its content is not in the message."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"{path} does not hold a device id ({LENGTH} characters of {ALPHABET})")


def path() -> Path:
    return config.settings.data_dir / DEVICE_ID_FILE


def generate() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def read() -> str:
    """The stored id. FileNotFoundError when there is none; DeviceIdInvalid when the file holds anything else."""
    p = path()
    with open(p, "rb") as f:
        raw = f.read(64)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise DeviceIdInvalid(p) from None
    if text.endswith("\n"):
        text = text[:-1]
    if PATTERN.match(text) is None or len(text) != LENGTH:
        raise DeviceIdInvalid(p)
    return text


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def create() -> str:
    """Write a new id unless one exists, and answer the stored one. Never replaces a file."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{DEVICE_ID_FILE}.", dir=p.parent)
    try:
        try:
            os.fchmod(fd, FILE_MODE)
            os.write(fd, generate().encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, p)
        except FileExistsError:
            pass
    finally:
        os.unlink(tmp)
    _fsync_dir(p.parent)
    return read()


def initialise() -> Optional[str]:
    """At start-up: the stored id, made now if there is none. None, with an ERROR logged, when it cannot be had."""
    try:
        return read()
    except FileNotFoundError:
        pass
    except DeviceIdInvalid as exc:
        logger.error("%s; the unit cannot ask Fisqua for a pairing code until it is repaired or removed", exc)
        return None
    except OSError as exc:
        logger.error("The device id in %s cannot be read (%s); the unit cannot ask Fisqua for a pairing code",
                     path(), type(exc).__name__)
        return None
    try:
        value = create()
    except (OSError, DeviceIdInvalid) as exc:
        logger.error("The device id could not be written to %s (%s)", path(), type(exc).__name__)
        return None
    logger.info("Device id written to %s", path())
    return value


def state() -> dict:
    """`deviceId` and `error`: None, `missing`, `invalid` or `unreadable`. Never writes."""
    try:
        return {"deviceId": read(), "error": None}
    except FileNotFoundError:
        return {"deviceId": None, "error": "missing"}
    except DeviceIdInvalid:
        return {"deviceId": None, "error": "invalid"}
    except OSError:
        return {"deviceId": None, "error": "unreadable"}
