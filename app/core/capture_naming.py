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
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.core.capture_counter import next_capture_number
from app.core.paths import PREVIEW_SIDECAR_SUFFIX, preview_sidecar_of

logger = logging.getLogger(__name__)

CAPTURE_NUMBER_DIGITS = 4

# The interface matches each refusal on its exact text, so these do not change.
CAPTURE_NAME_TOO_LONG_DETAIL = (
    "This capture's file name would be longer than the 36 characters a pair id holds. Nothing was captured."
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
    for d in dirs:
        try:
            with os.scandir(d) as entries:
                if any(carries(e.name) for e in entries):
                    return True
        except FileNotFoundError:
            continue
    return False


def take_filename_index(db: Session, collection_id: int, stem: str, output_dir: Path) -> str:
    """Raise the volume's capture counter and return the event's `<stem>-img_<nnnn>`.

    Called once per capture event or upload, before any camera is touched
    or any file is written, in the transaction that writes the event's
    images, so an event that fails before its commit leaves the counter
    where it was. The caller holds the volume's row lock.

    A number that a file in `output_dir` (the volume's `captures/`) or its
    `previews/` already carries (capture_name_taken) is passed over and the
    counter raised again, so no file is ever overwritten and no name is
    given twice. That happens only when the counter is behind the disk: an
    event whose files were written but whose commit failed, or a catalogue
    restored from before captures still on the card. The contract allows
    gaps in <nnnn> and forbids reuse.

    Raises CaptureNameRefused, leaving the rollback to the caller, when the
    index would be longer than PAIR_ID_MAX_LENGTH.
    """
    from app.models.collection import PAIR_ID_MAX_LENGTH

    while True:
        number: Optional[int] = next_capture_number(db, collection_id)
        if number is None:
            raise RuntimeError(f"Collection {collection_id} has no capture counter")
        index = capture_filename_index(stem, number)
        # A longer number only makes a longer name, so the first too long is final.
        if len(index) > PAIR_ID_MAX_LENGTH:
            logger.warning(f"Capture number {number} of collection {collection_id} makes {index}, "
                           f"longer than {PAIR_ID_MAX_LENGTH} characters; capture refused")
            raise CaptureNameRefused(CAPTURE_NAME_TOO_LONG_DETAIL)
        if not capture_name_taken(output_dir, index):
            return index
        logger.warning(f"Capture number {number} of collection {collection_id} is already on disk ({index}); taking the next")
