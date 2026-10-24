"""The names new captures and uploads are given (exchange contract 1.1, section 3).

Each image of a capture event is `<filename index>_c<camera index>.<ext>`,
and the filename index is `<filename_stem>-img_<nnnn>`. <nnnn> is the
volume's capture number (app.core.capture_counter), zero-padded to four
digits; str.format pads and never truncates, so 10000 and above are written
with five or more digits. The filename index doubles as a dual capture's
pair id, so it has to fit in record_images.pair_id (PAIR_ID_MAX_LENGTH, 36):
an index that would not is refused before any file is written, whatever the
capture mode, as the contract requires.

take_filename_index is the one place a capture or an upload takes its
number. The callers turn its CaptureNameRefused into a 409 carrying the
exception's fixed text, after rolling back, so a refused event consumes no
number.
"""
import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.core.capture_counter import next_capture_number
from app.core.paths import PREVIEW_SIDECAR_SUFFIX, preview_sidecar_of

logger = logging.getLogger(__name__)

CAPTURE_NUMBER_DIGITS = 4

# How many numbers already on disk take_filename_index passes over before it
# refuses the capture. More than a handful means the counter is far behind
# the card, which an operator cannot fix by trying again.
MAX_NUMBERS_PASSED_OVER = 100

# The interface matches each refusal on its exact text, so these do not change.
CAPTURE_NAME_TOO_LONG_DETAIL = (
    "This capture's file name would be longer than the 36 characters a pair id holds. Nothing was captured."
)
CAPTURE_NUMBERS_TAKEN_DETAIL = (
    "The next capture numbers of this volume are already taken by files on disk. Nothing was captured."
)
RESOLUTION_IN_FILENAME_DETAIL = (
    "A capture's file name cannot carry its resolution: the exchange contract fixes the name."
)


class CaptureNameRefused(Exception):
    """A capture or upload that cannot be given a contract name; `detail` is the fixed text of its 409."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def capture_filename_index(stem: str, number: int) -> str:
    """`<stem>-img_<nnnn>`: the shared part of a capture event's file names, and a dual capture's pair id."""
    if not stem:
        raise ValueError("A capture's file name needs its volume's file name stem")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError(f"Not a capture number: {number!r}")
    return f"{stem}-img_{number:0{CAPTURE_NUMBER_DIGITS}d}"


def capture_name_taken(output_dir: Path, name_stem: str) -> bool:
    """True if a file in `output_dir`, or in its sidecar directory, already carries `name_stem`.

    `name_stem` is a file name without its suffix (`<index>_c0`), or the
    filename index alone (`<index>`), which covers every camera of the event.
    A file carries it when its name, casefolded, is `name_stem` followed by
    a suffix, by `_c<n>` for an index, or by `_preview.jpg`. The suffix is
    not known before the shot (the gphoto2 backend writes `.cr2` for a
    request for `.jpg`), so every suffix counts. Names are casefolded
    because exFAT, vfat and APFS by default treat names differing only in
    case as one. The sidecar directory is where preview_sidecar_of puts a
    RAW master's `_preview.jpg`: `previews/` beside a `captures/` directory,
    the directory itself otherwise. A directory that does not exist holds
    nothing.
    """
    return bool(files_carrying(output_dir, name_stem, first_only=True))


def files_carrying(output_dir: Path, name_stem: str, first_only: bool = False) -> list:
    """The files in `output_dir` and its sidecar directory that carry `name_stem` (see capture_name_taken)."""
    target = name_stem.casefold()

    def carries(name: str) -> bool:
        name = name.casefold()
        if not name.startswith(target):
            return False
        rest = name[len(target):]
        return rest.startswith(".") or rest.startswith("_c") or rest.startswith(PREVIEW_SIDECAR_SUFFIX)

    dirs = [output_dir]
    sidecar_dir = preview_sidecar_of(output_dir / "x.jpg").parent
    if sidecar_dir != output_dir:
        dirs.append(sidecar_dir)
    found = []
    for d in dirs:
        try:
            with os.scandir(d) as entries:
                for e in entries:
                    if e.is_file() and carries(e.name):
                        found.append(Path(e.path))
                        if first_only:
                            return found
        except FileNotFoundError:
            continue
    return found


# Per manifest path and stem: (device, inode, bytes read, the numbers found so far).
# A manifest is append-only, so each call reads only what was appended since
# the last; a file replaced, or shorter than what was read, is read again.
_manifest_numbers_cache: Dict[Tuple[str, str], Tuple[int, int, int, Set[int]]] = {}
_manifest_numbers_lock = threading.Lock()


def manifest_capture_numbers(manifest: Path, stem: str) -> Set[int]:
    """The capture numbers a volume's manifest names, from any `<stem>-img_<nnnn>_c<n>` in it.

    A number named there was given to a capture once, even if its file has
    since been deleted from the card and its row from the catalogue, so it
    must not be given again (a catalogue restored from a backup holds an
    older counter). The text is searched for the name, not parsed as JSON,
    and only the bytes up to the last newline count, so a line still being
    appended is read on the next call. A manifest that does not exist names
    nothing.
    """
    pattern = re.compile(re.escape(stem.encode()) + rb"-img_(\d+)_c", re.IGNORECASE)
    key = (str(manifest), stem)
    with _manifest_numbers_lock:
        try:
            st = os.stat(manifest)
        except FileNotFoundError:
            _manifest_numbers_cache.pop(key, None)
            return set()
        dev, ino, offset, numbers = _manifest_numbers_cache.get(key, (st.st_dev, st.st_ino, 0, set()))
        if (dev, ino) != (st.st_dev, st.st_ino) or st.st_size < offset:
            offset, numbers = 0, set()
        if st.st_size > offset:
            with open(manifest, "rb") as f:
                f.seek(offset)
                chunk = f.read(st.st_size - offset)
            end = chunk.rfind(b"\n") + 1
            numbers = numbers | {int(m.group(1)) for m in pattern.finditer(chunk, 0, end)}
            offset += end
        _manifest_numbers_cache[key] = (st.st_dev, st.st_ino, offset, numbers)
        return set(numbers)


def take_filename_index(db: Session, collection_id: int, stem: str, output_dir: Path) -> str:
    """Raise the volume's capture counter and return the event's `<stem>-img_<nnnn>`.

    Called once per capture event or upload, before any camera is touched
    or any file is written, in the transaction that writes the event's
    images, so an event that fails before its commit leaves the counter
    where it was. The caller holds the volume's row lock.

    A number that a file in `output_dir` (the volume's `captures/`) or its
    `previews/` already carries (capture_name_taken), or that the volume's
    `metadata/manifest.jsonl` names (manifest_capture_numbers), is passed
    over and the counter raised again, so no file is ever overwritten and no
    name is given twice. That happens only when the counter is behind: an
    event whose files were written but whose commit failed, or a catalogue
    restored from a backup older than captures made since, including ones
    whose files have been deleted. The manifest is read once per call. The contract allows
    gaps in <nnnn> and forbids reuse.

    Raises CaptureNameRefused, leaving the rollback to the caller, when the
    index would be longer than PAIR_ID_MAX_LENGTH, or when more than
    MAX_NUMBERS_PASSED_OVER numbers in a row are already on disk.
    """
    from app.core.paths import METADATA_DIR
    from app.models.collection import PAIR_ID_MAX_LENGTH

    in_manifest = manifest_capture_numbers(output_dir.parent / METADATA_DIR / "manifest.jsonl", stem)
    for _ in range(MAX_NUMBERS_PASSED_OVER + 1):
        number: Optional[int] = next_capture_number(db, collection_id)
        if number is None:
            raise RuntimeError(f"Collection {collection_id} has no capture counter")
        index = capture_filename_index(stem, number)
        # A longer number only makes a longer name, so the first too long is final.
        if len(index) > PAIR_ID_MAX_LENGTH:
            logger.warning(f"Capture number {number} of collection {collection_id} makes {index}, "
                           f"longer than {PAIR_ID_MAX_LENGTH} characters; capture refused")
            raise CaptureNameRefused(CAPTURE_NAME_TOO_LONG_DETAIL)
        if number not in in_manifest and not capture_name_taken(output_dir, index):
            return index
        logger.warning(f"Capture number {number} of collection {collection_id} is already on disk or in the manifest ({index}); taking the next")
    raise CaptureNameRefused(CAPTURE_NUMBERS_TAKEN_DETAIL)
