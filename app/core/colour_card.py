"""Colour card shots in a volume (NEH-340): when they are asked for, taking them, and what a volume holds.

A colour card shot is a photograph of a colour reference card, one per
camera, taken under the light and settings of the volume's pages, so that
the volume's colours can be corrected later. "Calibration" names the camera
and white balance calibration elsewhere (capture/calibration.py); it is
used here only where the Fisqua contract fixes the word (the `calibration/`
folder, the manifest's fields, contract 1.12 §13).

**When the unit asks.** Only while the unit's setting is on
(app.core.unit_mode.colour_card_shots) and never in a testing volume
(enabled). A volume is asked for a set (needed):

- `start`: it has no completed set and no page capture yet;
- `begun_without`: it has no completed set and has page captures (a volume
  begun before the setting was on), once: a set taken or a request skipped
  answers it;
- `settings_changed`: a setting compared for the cameras' driver differs
  from what the latest completed set recorded for that camera (COMPARED).
  Only the settings both sides have are compared. A camera whose settings
  cannot be read makes the answer unknown (None), never true.

**Settings, per driver** (read_settings). What a set records for each
camera, and what is compared:

- gphoto2 (a DSLR over PTP): `iso`, `shutter_speed`, `aperture` and
  `image_format`, as GET /cameras/dslr/{i}/settings reads them from the
  camera. Compared: `iso`, `shutter_speed`, `aperture`. Without reading the
  camera they are unknown, so a stored-only answer for a volume that has a
  set is None.
- chdk (a compact): `zoom_step`, the step the body last confirmed, held by
  the backend (ChdkBackend.held_zoom), and `image_format`, the stored
  capture format. Neither needs the camera. Compared: `zoom_step`. A CHDK
  body's shutter speed and ISO are not read back.
- picamera2 and rpicam-subprocess: nothing; the unit sets their exposure
  automatically and holds no zoom. Nothing is compared, so their sets are
  asked for at the start only.

A shot records more (shot_settings): the camera settings above, read just
before it is taken; the backend's capture metadata (picamera2's exposure
time, analogue gain, colour gains, colour temperature, lens position); and
the EXIF of the JPEG written (exposure time, f-number, ISO, focal length,
white balance), where the file carries it.

**Status** (status). `present` when the volume has a taken set, no request
was skipped, every shot of its taken sets can be delivered (its files on
disk, no upload of it refused), and every current page image was captured
after the first taken set (its id is above the set's after_image_id); `skipped` when a set
exists otherwise; `none` with no completed set. Image ids give the order
because the unit's clock cannot be relied on after a power cut.

**Files.** A shot is `<stem>-cal_<nn>_c<n>.<ext>` in `<stem>/calibration/`
(`nn`, the set's number, two digits at least), and a RAW master's preview is
`<same stem>_preview.jpg` beside it (app.core.paths.preview_sidecar_of puts
it there, since the folder is not `captures/`). The backend writes under a
temporary name in the same folder, which is renamed to the shot's name once
the photograph is on disk, replacing the camera's earlier shot in the set.
Each shot appends a `calibration` line to the volume's manifest
(capture.manifestHandler.append_calibration_record).
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

START, BEGUN_WITHOUT, SETTINGS_CHANGED = "start", "begun_without", "settings_changed"
PRESENT, SKIPPED, NONE = "present", "skipped", "none"
TAKEN = "taken"

# What is compared for "settings changed", per driver (module header).
COMPARED: Dict[str, Tuple[str, ...]] = {
    "gphoto2": ("iso", "shutter_speed", "aperture"),
    "chdk": ("zoom_step",),
}

NOT_ENABLED = "colour_card_not_enabled"
NOT_ENABLED_MESSAGE = "Colour card shots are not asked for in this volume."
STEM_REQUIRED = "stem_required"
STEM_REQUIRED_MESSAGE = "This volume has no file name stem. Give it one before taking colour card shots in it."
SET_NOT_FOUND = "colour_card_set_not_found"
SET_NOT_FOUND_MESSAGE = "This volume has no such set of colour card shots."
SET_CLOSED = "colour_card_set_closed"
SET_CLOSED_MESSAGE = "This set of colour card shots is already finished."
CAMERA_NOT_IN_SET = "colour_card_camera_not_in_set"
CAMERA_NOT_IN_SET_MESSAGE = "This camera takes no shot in this set."
SHOTS_MISSING = "colour_card_shots_missing"
SHOTS_MISSING_MESSAGE = "Every camera must take its shot before the set is finished."
CAMERA_BUSY = "camera_busy"
CAMERA_BUSY_MESSAGE = "This camera is taking another colour card shot."
CAMERA_NOT_CONNECTED = "camera_not_connected"
CAMERA_NOT_CONNECTED_MESSAGE = "This camera is not connected."
CAPTURE_FAILED = "colour_card_capture_failed"
CAPTURE_FAILED_MESSAGE = "The camera did not take the colour card shot."
SHOT_NOT_FOUND = "colour_card_shot_not_found"
SHOT_NOT_FOUND_MESSAGE = "This volume has no such colour card shot."
NO_PREVIEW = "colour_card_no_preview"
NO_PREVIEW_MESSAGE = "This colour card shot has no image a browser can show."


def refusal(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Whether a volume is asked
# ---------------------------------------------------------------------------

def setting_on(db: Session) -> bool:
    from app.core import unit_mode

    return unit_mode.colour_card_shots(db)


def enabled(db: Session, volume, setting: Optional[bool] = None) -> bool:
    """The unit's setting is on and the volume is not testing work."""
    if bool(getattr(volume, "testing", False)):
        return False
    return setting_on(db) if setting is None else bool(setting)


def _sets(db: Session, collection_id: int) -> list:
    from app.models.colour_card import ColourCardSet

    return (db.query(ColourCardSet).filter(ColourCardSet.collection_id == collection_id)
            .order_by(ColourCardSet.number).all())


def _completed(sets) -> list:
    return [s for s in sets if s.outcome is not None]


def _page_image_ids(db: Session, collection_id: int) -> List[int]:
    """The ids of the volume's current page images, in capture order."""
    from app.models.record import Record, RecordImage

    return [i for (i,) in db.query(RecordImage.id)
            .join(Record, RecordImage.record_id == Record.id)
            .filter(Record.collection_id == collection_id, RecordImage.is_current.is_(True))
            .order_by(RecordImage.id).all()]


def _has_pages(db: Session, collection_id: int) -> bool:
    from app.models.record import Record, RecordImage

    return db.query(RecordImage.id).join(Record, RecordImage.record_id == Record.id).filter(
        Record.collection_id == collection_id).first() is not None


def _status_of(completed, first_page_id: Optional[int], undeliverable=frozenset()) -> str:
    if not completed:
        return NONE
    if any(s.outcome != TAKEN or s.id in undeliverable for s in completed):
        return SKIPPED
    first = min(s.after_image_id or 0 for s in completed)
    if first_page_id is not None and first_page_id <= first:
        return SKIPPED
    return PRESENT


def status(db: Session, volume) -> str:
    """`present`, `skipped` or `none` (module header)."""
    ids = _page_image_ids(db, volume.id)
    return _status_of(_completed(_sets(db, volume.id)), ids[0] if ids else None, undeliverable_sets(db, volume.id))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _backend():
    from capture.service import get_backend

    return get_backend()


def read_settings(camera_index: int, *, read_camera: bool) -> Optional[dict]:
    """The camera's settings the driver can give (module header), or None when they cannot be read.

    With `read_camera` false nothing is sent to a camera: a gphoto2 camera's
    exposure is then unknown (None).
    """
    try:
        backend = _backend()
        name = backend.get_backend_name()
        if name == "gphoto2":
            if not read_camera:
                return None
            raw = backend.get_dslr_settings(camera_index)
            return {k: raw.get(k) for k in ("iso", "shutter_speed", "aperture", "image_format")}
        if name == "chdk":
            held = backend.held_zoom(camera_index)
            if held is None:
                return None
            fmt = backend.capture_image_format(camera_index)
            return {"zoom_step": held.get("zoom_step"), "image_format": fmt if isinstance(fmt, str) else "JPEG"}
        return {}
    except Exception as exc:  # a camera that does not answer is unknown, never a failure of the request
        logger.warning(f"Colour card: the settings of camera {camera_index} could not be read: {exc!r}")
        return None


def driver_name() -> Optional[str]:
    try:
        return _backend().get_backend_name()
    except Exception:
        return None


def settings_differ(recorded: Optional[dict], current: Optional[dict], keys: Iterable[str]) -> bool:
    """Whether a compared setting both sides have differs."""
    if not recorded or not current:
        return False
    for key in keys:
        a, b = recorded.get(key), current.get(key)
        if a is not None and b is not None and str(a) != str(b):
            return True
    return False


def needed(db: Session, volume, *, read_cameras: bool, setting: Optional[bool] = None) -> tuple:
    """(needed, reason): True with `start`, `begun_without` or `settings_changed`; False; or None when unknown."""
    if not enabled(db, volume, setting):
        return False, None
    completed = _completed(_sets(db, volume.id))
    if not completed:
        return True, (BEGUN_WITHOUT if _has_pages(db, volume.id) else START)
    latest = completed[-1]
    keys = COMPARED.get(driver_name() or "", ())
    if not keys:
        return False, None
    unknown = False
    for cam in latest.cameras or []:
        current = read_settings(int(cam.get("camera_index", 0)), read_camera=read_cameras)
        if current is None:
            unknown = True
            continue
        if settings_differ(cam.get("settings"), current, keys):
            return True, SETTINGS_CHANGED
    if unknown:
        return None, None
    return False, None


def _exif_settings(path: Path) -> dict:
    """Exposure time, f-number, ISO, focal length and white balance from a JPEG's EXIF; empty when it has none."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            exif = img.getexif()
            sub = exif.get_ifd(0x8769) if exif else {}
    except Exception:
        return {}
    names = {33434: "exposure_time", 33437: "f_number", 34855: "iso", 37386: "focal_length", 41987: "white_balance"}
    out = {}
    for tag, name in names.items():
        value = sub.get(tag) if sub else None
        if value is None:
            continue
        if isinstance(value, (tuple, list)):
            value = value[0] if value else None
        try:
            out[name] = float(value) if not isinstance(value, int) else value
        except (TypeError, ValueError):
            out[name] = str(value)
    return out


_METADATA_NAMES = {"ExposureTime": "exposure_time_us", "AnalogueGain": "analogue_gain",
                   "DigitalGain": "digital_gain", "ColourGains": "colour_gains",
                   "ColourTemperature": "colour_temperature", "LensPosition": "lens_position"}


def shot_settings(camera: Optional[dict], metadata, displayed: Optional[Path]) -> dict:
    """What a shot records (module header): {"driver", "camera", "capture", "exif"}."""
    capture = {}
    if isinstance(metadata, dict):
        for key, name in _METADATA_NAMES.items():
            if key in metadata:
                value = metadata[key]
                capture[name] = list(value) if isinstance(value, (tuple, list)) else value
    return {"driver": driver_name(), "camera": camera, "capture": capture,
            "exif": _exif_settings(displayed) if displayed is not None else {}}


# ---------------------------------------------------------------------------
# Serialised
# ---------------------------------------------------------------------------

def _is_raw_name(name: str) -> bool:
    from app.core.paths import is_raw_suffix

    return is_raw_suffix(Path(name))


def shot_url(collection_id: int, shot_id: int) -> str:
    return f"/collections/{collection_id}/colour-card/shots/{shot_id}/preview"


def shot_dict(shot, collection_id: int) -> dict:
    return {
        "id": shot.id,
        "camera_index": shot.camera_index,
        "hardware_id": shot.hardware_id,
        "role": shot.role,
        "capture_id": shot.capture_id,
        "filename": shot.filename,
        "sha256": shot.sha256,
        "preview_filename": shot.preview_filename,
        "preview_sha256": shot.preview_sha256,
        # Whether a browser can be shown the shot: a JPEG master, or a RAW
        # master whose preview was extracted when it was taken.
        "preview": bool(shot.preview_path) or not _is_raw_name(shot.filename),
        "rotate_deg": shot.rotate_deg,
        "captured_at": shot.captured_at.isoformat() if shot.captured_at else None,
        "captured_by": shot.captured_by,
        "settings": shot.settings,
        "url": shot_url(collection_id, shot.id),
    }


def delivery(db: Session, card_set) -> tuple:
    """(delivery, delivery_reason) of a taken set; (None, None) for an open or skipped set.

    `sent`: every object verified offsite. `waiting`, with the reason the
    uploader last skipped one of its objects for, or None when none was
    skipped. `undeliverable`, with `file_missing` or `upload_refused` (an
    upload refused, which a skip repeated image_upload.SKIP_LIMIT times
    becomes).
    """
    if card_set.outcome != TAKEN:
        return None, None
    shots = list(card_set.shots)
    rows = _objects(db, [s.id for s in shots])
    for shot in shots:
        reason = _undeliverable_reason(shot, rows)
        if reason is not None:
            return "undeliverable", reason
    verified = all(_verified(rows.get((s.id, "shot"))) and (not s.preview_path or _verified(rows.get((s.id, "preview"))))
                   for s in shots)
    if verified:
        return "sent", None
    # Why it waits, when the uploader skipped an object of it
    # (app.core.image_upload: staging_failed, file_unstable,
    # listed_without_sha1, unverified); None while nothing was tried yet.
    skipped = next((row.last_skip_reason for s in shots for k in ("shot", "preview")
                    for row in [rows.get((s.id, k))] if row is not None and row.last_skip_reason), None)
    return "waiting", skipped


def set_dict(card_set) -> dict:
    from sqlalchemy.orm import object_session

    state, reason = delivery(object_session(card_set), card_set)
    return {
        "id": card_set.id,
        "number": card_set.number,
        "outcome": card_set.outcome,
        "capture_mode": card_set.capture_mode,
        "cameras": card_set.cameras,
        "opened_at": card_set.opened_at.isoformat() if card_set.opened_at else None,
        "opened_by": card_set.opened_by,
        "completed_at": card_set.completed_at.isoformat() if card_set.completed_at else None,
        "completed_by": card_set.completed_by,
        "shots": [shot_dict(s, card_set.collection_id) for s in card_set.shots],
        # Whether the taken set's shots are offsite (evaluate's limit: a set
        # that could not be delivered is carried by the volume's next handover).
        "delivery": state,
        "delivery_reason": reason,
    }


def volume_answer(db: Session, volume, *, read_cameras: bool) -> dict:
    """The GET /collections/{id}/colour-card body."""
    if bool(getattr(volume, "testing", False)):
        return {"enabled": False, "needed": False, "reason": None, "status": NONE, "sets": []}
    on = enabled(db, volume)
    is_needed, reason = needed(db, volume, read_cameras=read_cameras) if on else (False, None)
    return {"enabled": on, "needed": is_needed, "reason": reason, "status": status(db, volume),
            "sets": [set_dict(s) for s in _sets(db, volume.id)]}


def summaries(db: Session, volumes) -> Dict[int, dict]:
    """The `colour_card` object of each volume in a list or detail answer, from the database alone.

    A fixed number of queries whatever the number of volumes, and no file is
    looked at: the status is from the rows (sets taken and skipped, pages
    before the first set), so it can read `present` for a volume whose shot
    file has since gone missing. GET /collections/{id}/colour-card, the
    manifest and integrity look at the files and report it.
    """
    from app.models.colour_card import ColourCardSet
    from app.models.record import Record, RecordImage

    volumes = list(volumes)
    setting = setting_on(db)
    out: Dict[int, dict] = {}
    asked = [v.id for v in volumes if enabled(db, v, setting)]
    for v in volumes:
        out[v.id] = {"enabled": False}
    if not asked:
        return out
    sets: Dict[int, list] = {}
    for s in (db.query(ColourCardSet).filter(ColourCardSet.collection_id.in_(asked),
                                             ColourCardSet.outcome.isnot(None))
              .order_by(ColourCardSet.number)):
        sets.setdefault(s.collection_id, []).append(s)
    firsts = dict(db.query(Record.collection_id, func.min(RecordImage.id))
                  .join(RecordImage, RecordImage.record_id == Record.id)
                  .filter(Record.collection_id.in_(asked), RecordImage.is_current.is_(True))
                  .group_by(Record.collection_id).all())
    for cid in asked:
        completed = sets.get(cid, [])
        out[cid] = {"enabled": True, "status": _status_of(completed, firsts.get(cid)),
                    "sets": sum(1 for s in completed if s.outcome == TAKEN)}
    return out


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def _locked_volume(db: Session, collection_id: int):
    from app.core.volume_stem import locked_volume_query

    volume = locked_volume_query(db, collection_id).first()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    return volume


def _refuse_unless_asked(db: Session, volume) -> None:
    if not enabled(db, volume):
        db.rollback()
        raise refusal(409, NOT_ENABLED, NOT_ENABLED_MESSAGE)
    if volume.filename_stem is None:
        db.rollback()
        raise refusal(409, STEM_REQUIRED, STEM_REQUIRED_MESSAGE)


def _capture_mode(db: Session, volume, requested: Optional[str]) -> str:
    """The set's mode: the one asked for, else the volume's latest record's, else dual when camera 1 is connected."""
    if requested in ("single", "dual"):
        return requested
    from app.models.record import Record

    latest = (db.query(Record.capture_mode).filter(Record.collection_id == volume.id)
              .order_by(Record.id.desc()).first())
    if latest is not None and latest[0] in ("single", "dual"):
        return latest[0]
    try:
        from capture.service import is_camera_connected

        return "dual" if is_camera_connected(1) else "single"
    except Exception:
        return "single"


def _camera_identity(camera_index: int, capture_mode: str) -> tuple:
    """(hardware_id, role) of the camera at the index, from the camera registry; role `single` in a single set."""
    hw_id, role = None, None
    try:
        from capture.camera_registry import CameraRegistry

        hw_id, data = CameraRegistry().get_camera_by_index(camera_index)
        role = (data or {}).get("role")
    except Exception as exc:
        logger.warning(f"Colour card: camera {camera_index}'s identity could not be read: {exc!r}")
    if capture_mode == "single":
        role = "single"
    return hw_id, role if role in ("left", "right", "single") else None


def _cameras(capture_mode: str, camera_index: Optional[int]) -> List[int]:
    if capture_mode == "dual":
        return [0, 1]
    return [camera_index if camera_index is not None else 0]


def _camera_entries(capture_mode: str, camera_index: Optional[int]) -> List[dict]:
    entries = []
    for index in _cameras(capture_mode, camera_index):
        hw_id, role = _camera_identity(index, capture_mode)
        entries.append({"camera_index": index, "hardware_id": hw_id, "role": role,
                        "settings": read_settings(index, read_camera=True)})
    return entries


def _open_set(db: Session, collection_id: int):
    from app.models.colour_card import ColourCardSet

    return (db.query(ColourCardSet).filter(ColourCardSet.collection_id == collection_id,
                                           ColourCardSet.outcome.is_(None))
            .order_by(ColourCardSet.number.desc()).first())


def _next_number(db: Session, collection_id: int) -> int:
    from app.models.colour_card import ColourCardSet

    top = db.query(func.max(ColourCardSet.number)).filter(ColourCardSet.collection_id == collection_id).scalar()
    return (top or 0) + 1


def _last_image_id(db: Session) -> int:
    from app.models.record import RecordImage

    return db.query(func.max(RecordImage.id)).scalar() or 0


def open_set(db: Session, collection_id: int, user, capture_mode: Optional[str] = None,
             camera_index: Optional[int] = None):
    """The volume's open set, or a new one recording the cameras' current settings; committed."""
    from app.models.colour_card import ColourCardSet

    volume = _locked_volume(db, collection_id)
    _refuse_unless_asked(db, volume)
    existing = _open_set(db, collection_id)
    if existing is not None:
        db.commit()
        return existing
    mode = _capture_mode(db, volume, capture_mode)
    card_set = ColourCardSet(collection_id=collection_id, number=_next_number(db, collection_id),
                             capture_mode=mode, cameras=_camera_entries(mode, camera_index),
                             opened_at=_now(), opened_by=user.username)
    db.add(card_set)
    db.commit()
    db.refresh(card_set)
    logger.info(f"Colour card: set {card_set.number} opened in volume {collection_id} ({mode})")
    return card_set


def _remove_files(paths: Iterable[Optional[str]]) -> None:
    from app.core.paths import resolve_within_storage

    for raw in paths:
        p = resolve_within_storage(raw) if raw else None
        if p is None:
            continue
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"Colour card: could not remove {p}: {exc}")


def _shot_files(shot) -> list:
    from app.core.paths import raw_sidecar_of

    files = [shot.file_path, shot.preview_path]
    if shot.file_path and not shot.file_path.lower().endswith(".raw"):
        files.append(str(raw_sidecar_of(Path(shot.file_path))))
    return files


def skip(db: Session, collection_id: int, user, capture_mode: Optional[str] = None,
         camera_index: Optional[int] = None):
    """Record a skipped request: the open set, its shots removed, or a new set with the current settings; committed."""
    from app.models.colour_card import ColourCardSet

    volume = _locked_volume(db, collection_id)
    _refuse_unless_asked(db, volume)
    card_set = _open_set(db, collection_id)
    removed = []
    if card_set is None:
        mode = _capture_mode(db, volume, capture_mode)
        card_set = ColourCardSet(collection_id=collection_id, number=_next_number(db, collection_id),
                                 capture_mode=mode, cameras=_camera_entries(mode, camera_index),
                                 opened_at=_now(), opened_by=user.username)
        db.add(card_set)
    else:
        for shot in list(card_set.shots):
            removed.extend(_shot_files(shot))
            card_set.shots.remove(shot)
    card_set.outcome = "skipped"
    card_set.completed_at = _now()
    card_set.completed_by = user.username
    card_set.after_image_id = _last_image_id(db)
    db.commit()
    db.refresh(card_set)
    _remove_files(removed)
    logger.info(f"Colour card: set {card_set.number} skipped in volume {collection_id} by {user.username}")
    return card_set


def _set_of(db: Session, collection_id: int, set_id: int):
    from app.models.colour_card import ColourCardSet

    card_set = db.get(ColourCardSet, set_id)
    if card_set is None or card_set.collection_id != collection_id:
        db.rollback()
        raise refusal(404, SET_NOT_FOUND, SET_NOT_FOUND_MESSAGE)
    return card_set


def complete(db: Session, collection_id: int, set_id: int, user):
    """Mark the set taken; refused unless every camera of its mode has a shot. Committed."""
    volume = _locked_volume(db, collection_id)
    _refuse_unless_asked(db, volume)
    card_set = _set_of(db, collection_id, set_id)
    if card_set.outcome is not None:
        db.rollback()
        raise refusal(409, SET_CLOSED, SET_CLOSED_MESSAGE)
    have = {s.camera_index for s in card_set.shots}
    wanted = [int(c["camera_index"]) for c in card_set.cameras or []]
    missing = [i for i in wanted if i not in have]
    if missing:
        db.rollback()
        raise refusal(409, SHOTS_MISSING, SHOTS_MISSING_MESSAGE, missing_camera_indexes=missing)
    card_set.outcome = TAKEN
    card_set.completed_at = _now()
    card_set.completed_by = user.username
    card_set.after_image_id = _last_image_id(db)
    db.commit()
    db.refresh(card_set)
    logger.info(f"Colour card: set {card_set.number} taken in volume {collection_id}")
    return card_set


# One shot at a time per camera in this process; a page capture on the same
# camera waits on the backend's own lock instead.
_camera_guards: Dict[int, threading.Lock] = {}
_camera_guards_mutex = threading.Lock()


def _camera_guard(camera_index: int) -> threading.Lock:
    with _camera_guards_mutex:
        return _camera_guards.setdefault(camera_index, threading.Lock())


def shot_stem(stem: str, number: int, camera_index: int) -> str:
    return f"{stem}-cal_{number:02d}_c{camera_index}"


def take_shot(db: Session, collection_id: int, set_id: int, camera_index: int, user,
              resolution: str = "high"):
    """Photograph the colour card with one camera of an open set; returns the shot row (module header).

    Under the volume's row lock, held through the shot as a page capture
    holds it, and the backend's lock on the camera. A second shot of the
    camera in the set replaces the first: its file is replaced, its row
    updated with a new capture id.
    """
    from app.api.cameras import _put_capture_format
    from app.core import storage_override
    from app.core.paths import CALIBRATION_DIR, PREVIEW_SIDECAR_SUFFIX, is_raw_suffix, preview_sidecar_of, raw_sidecar_of
    from app.models.colour_card import ColourCardShot
    from capture.camera import CameraConfig, recorded_rotation
    from capture.manifestHandler import append_calibration_record
    from capture.project_manager import default_camera_config_from_registry, projects_root, volume_root
    from capture.service import colour_card_capture, is_camera_connected
    from capture.utils import compute_sha256

    volume = _locked_volume(db, collection_id)
    _refuse_unless_asked(db, volume)
    card_set = _set_of(db, collection_id, set_id)
    if card_set.outcome is not None:
        db.rollback()
        raise refusal(409, SET_CLOSED, SET_CLOSED_MESSAGE)
    entry = next((c for c in card_set.cameras or [] if int(c.get("camera_index", -1)) == camera_index), None)
    if entry is None:
        db.rollback()
        raise refusal(422, CAMERA_NOT_IN_SET, CAMERA_NOT_IN_SET_MESSAGE)
    guard = _camera_guard(camera_index)
    if not guard.acquire(blocking=False):
        db.rollback()
        raise refusal(409, CAMERA_BUSY, CAMERA_BUSY_MESSAGE)
    written: List[Path] = []
    try:
        if not is_camera_connected(camera_index):
            db.rollback()
            raise refusal(409, CAMERA_NOT_CONNECTED, CAMERA_NOT_CONNECTED_MESSAGE)
        config_dict, hw_id = default_camera_config_from_registry(camera_index, resolution)
        _put_capture_format(db, config_dict, camera_index, collection_id)
        camera_config = CameraConfig(**config_dict)
        root = projects_root()
        folder = volume_root(volume.filename_stem, root) / CALIBRATION_DIR
        try:
            storage_override.refuse_below_storage_reserve(root)
        except HTTPException:
            db.rollback()
            raise
        camera_settings = read_settings(camera_index, read_camera=True)
        final_stem = shot_stem(volume.filename_stem, card_set.number, camera_index)
        temp = folder / f"{final_stem}.part-{secrets.token_hex(4)}.{camera_config.encoding.lstrip('.')}"
        try:
            paths, metadata = colour_card_capture(camera_config, temp)
        except Exception as exc:
            logger.exception(f"Colour card shot failed on camera {camera_index}: {exc}")
            written = [temp, preview_sidecar_of(temp), raw_sidecar_of(temp)]
            db.rollback()
            raise refusal(502, CAPTURE_FAILED, CAPTURE_FAILED_MESSAGE)
        master_tmp = paths[0]
        preview_tmp = preview_sidecar_of(master_tmp)
        written = list(paths) + [preview_tmp]
        has_preview = is_raw_suffix(master_tmp) and preview_tmp.is_file()
        master = folder / f"{final_stem}{master_tmp.suffix}"
        preview = folder / f"{final_stem}{PREVIEW_SIDECAR_SUFFIX}"
        moves = [(master_tmp, master)] + [(extra, folder / f"{final_stem}{extra.suffix}") for extra in paths[1:]]
        if has_preview:
            moves.append((preview_tmp, preview))
        shot = (db.query(ColourCardShot)
                .filter(ColourCardShot.set_id == card_set.id, ColourCardShot.camera_index == camera_index)
                .one_or_none())
        try:
            return _save_shot(db, shot, card_set, camera_index, entry, hw_id, user, camera_config, metadata,
                              camera_settings, volume, root, folder, moves, master, preview, has_preview,
                              master_tmp, preview_tmp)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(f"Colour card shot on camera {camera_index} was not saved: {exc}")
            raise refusal(502, CAPTURE_FAILED, CAPTURE_FAILED_MESSAGE)
    finally:
        guard.release()
        for leftover in written:
            try:
                Path(leftover).unlink(missing_ok=True)
            except OSError:
                pass


def _backup_name(path: Path) -> Path:
    """`<stem>.old-<8 hex><suffix>` beside `path`: where a shot's earlier file waits while its replacement is saved."""
    return path.with_name(f"{path.stem}.old-{secrets.token_hex(4)}{path.suffix}")


def _save_shot(db, shot, card_set, camera_index, entry, hw_id, user, camera_config, metadata, camera_settings,
               volume, root, folder, moves, master, preview, has_preview, master_tmp, preview_tmp):
    """Save a shot written under its temporary names; the camera's earlier shot is removed only once this one is committed.

    The order, so that a failure at any step leaves the earlier shot's row
    and files as they were:

    1. the new files are hashed under their temporary names;
    2. each file of the earlier shot is renamed to a backup name
       (`<name>.old-<hex>`) in the same folder;
    3. the new files are renamed to the shot's names;
    4. the shot's manifest line is appended and the row updated (new
       capture id, names, digests) in one transaction, which is committed;
    5. the backups are removed.

    A failure in 1 to 4 rolls the row back, removes the new files placed in
    3, renames the backups back and removes the temporary files. The line of
    a failed commit stays in the append-only manifest, naming a capture id
    no row has; such a line makes no file known (app.core.integrity).

    Between 3 and the commit the row still holds the earlier shot's digest
    while the new file is at its path. A crash in that window leaves the new
    bytes at the path, the earlier row and the earlier bytes in the backup:
    integrity reports the shot's digest mismatch (colour_card_mismatches)
    and the backup as an orphan file. A crash between 2 and 3 leaves no file
    at the row's path, reported as a missing shot file, with the backup as
    an orphan.
    """
    from app.core.paths import CALIBRATION_DIR, is_raw_suffix
    from app.models.colour_card import ColourCardShot
    from capture.camera import recorded_rotation
    from capture.manifestHandler import append_calibration_record
    from capture.project_manager import volume_root
    from capture.utils import compute_sha256

    sha = compute_sha256(str(master_tmp))
    preview_sha = compute_sha256(str(preview_tmp)) if has_preview else None
    backups: List[Tuple[Path, Path]] = []
    placed: List[Path] = []
    try:
        if shot is not None:
            from app.core.paths import resolve_within_storage
            for raw in _shot_files(shot):
                old = resolve_within_storage(raw) if raw else None
                if old is not None and old.is_file():
                    backup = _backup_name(old)
                    os.replace(old, backup)
                    backups.append((old, backup))
        for src, dst in moves:
            os.replace(src, dst)
            placed.append(dst)
        rotate = recorded_rotation(camera_config)
        capture_id = str(uuid.uuid4())
        settings = shot_settings(camera_settings, metadata, preview if has_preview else master)
        hw_identity, role = entry.get("hardware_id") or hw_id, entry.get("role")
        files = [{"role": "calibration", "relative_path": f"{CALIBRATION_DIR}/{master.name}",
                  "bytes": master.stat().st_size, "mimetype": "image/jpeg" if not is_raw_suffix(master)
                  else "application/octet-stream", "sha256": sha, "rotate_deg": rotate}]
        if has_preview:
            files.append({"role": "calibration_preview", "relative_path": f"{CALIBRATION_DIR}/{preview.name}",
                          "bytes": preview.stat().st_size, "mimetype": "image/jpeg", "sha256": preview_sha,
                          "rotate_deg": rotate})
        fields = dict(hardware_id=hw_identity, role=role, capture_id=capture_id, filename=master.name,
                      file_path=str(master), file_size=master.stat().st_size, sha256=sha,
                      preview_filename=preview.name if has_preview else None,
                      preview_path=str(preview) if has_preview else None,
                      preview_sha256=preview_sha, rotate_deg=rotate, captured_at=_now(),
                      captured_by=user.username, settings=settings)
        if shot is None:
            shot = ColourCardShot(set_id=card_set.id, camera_index=camera_index, **fields)
            db.add(shot)
        else:
            for name, value in fields.items():
                setattr(shot, name, value)
        append_calibration_record(volume_root(volume.filename_stem, root), {
            "capture_id": capture_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "set_number": card_set.number,
            "camera_index": camera_index,
            "hardware_id": hw_identity,
            "role": role,
            "files": files,
            "cameras": [{"camera_index": camera_index, "config": camera_config.to_dict(),
                         "camera_controls_metadata": metadata if isinstance(metadata, dict) else None}],
            "settings": settings,
        })
        db.commit()
    except Exception:
        db.rollback()
        for dst in placed:
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
        for old, backup in backups:
            try:
                os.replace(backup, old)
            except OSError as exc:
                logger.error(f"Colour card: the earlier shot {old} could not be put back from {backup}: {exc}")
        raise
    for _old, backup in backups:
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"Colour card: could not remove {backup}: {exc}")
    db.refresh(shot)
    logger.info(f"Colour card: shot {master.name} taken in volume {volume.id}, capture_id={shot.capture_id}")
    return shot


def shot_of(db: Session, collection_id: int, shot_id: int):
    from app.models.colour_card import ColourCardSet, ColourCardShot

    shot = db.get(ColourCardShot, shot_id)
    if shot is None:
        raise refusal(404, SHOT_NOT_FOUND, SHOT_NOT_FOUND_MESSAGE)
    card_set = db.get(ColourCardSet, shot.set_id)
    if card_set is None or card_set.collection_id != collection_id:
        raise refusal(404, SHOT_NOT_FOUND, SHOT_NOT_FOUND_MESSAGE)
    return shot


# ---------------------------------------------------------------------------
# A volume's shots on disk
# ---------------------------------------------------------------------------

def volume_shots(db: Session, collection_id: int) -> list:
    """Every shot of the volume, in set and camera order."""
    from app.models.colour_card import ColourCardSet, ColourCardShot

    return (db.query(ColourCardShot).join(ColourCardSet, ColourCardShot.set_id == ColourCardSet.id)
            .filter(ColourCardSet.collection_id == collection_id)
            .order_by(ColourCardSet.number, ColourCardShot.camera_index).all())


def shot_file_paths(db: Session, collection_id: int) -> List[Path]:
    """The resolved paths of every file of the volume's shots (master, preview, raw buffer), as stored."""
    from app.core.paths import resolve_within_storage

    out = []
    for shot in volume_shots(db, collection_id):
        for raw in _shot_files(shot):
            p = resolve_within_storage(raw) if raw else None
            if p is not None:
                out.append(p)
    return out


def remove_volume_shots(db: Session, collection_id: int) -> list:
    """Delete the volume's sets and shots (in the caller's transaction); return the files to remove after it commits."""
    files: list = []
    for card_set in _sets(db, collection_id):
        for shot in card_set.shots:
            files.extend(_shot_files(shot))
        db.delete(card_set)
    return files


remove_files = _remove_files


# ---------------------------------------------------------------------------
# Offsite and the Fisqua manifest (contract 1.12 §13)
# ---------------------------------------------------------------------------

def _taken_shots(db: Session, collection_id: int) -> list:
    """(set, shot) for every shot of a taken set, in set and camera order."""
    from app.models.colour_card import ColourCardSet, ColourCardShot

    return (db.query(ColourCardSet, ColourCardShot)
            .join(ColourCardShot, ColourCardShot.set_id == ColourCardSet.id)
            .filter(ColourCardSet.collection_id == collection_id, ColourCardSet.outcome == TAKEN)
            .order_by(ColourCardSet.number, ColourCardShot.camera_index).all())


def _objects(db: Session, shot_ids) -> dict:
    from app.models.colour_card import ColourCardObject

    ids = list(shot_ids)
    if not ids:
        return {}
    return {(o.shot_id, o.kind): o for o in db.query(ColourCardObject).filter(ColourCardObject.shot_id.in_(ids))}


def _verified(row) -> bool:
    return row is not None and row.verified_at is not None


def _applies(card_set, completed, page_ids) -> Optional[int]:
    """The first page image id the set's shots apply to: above its after_image_id, at most the next completed set's."""
    later = [s for s in completed if s.number > card_set.number]
    start = card_set.after_image_id or 0
    end = later[0].after_image_id if later else None
    return next((i for i in page_ids if i > start and (end is None or i <= end)), None)


def undeliverable_sets(db: Session, collection_id: int) -> set:
    """The ids of the volume's taken sets with a shot that cannot be delivered (_undeliverable_reason)."""
    pairs = _taken_shots(db, collection_id)
    rows = _objects(db, [shot.id for _s, shot in pairs])
    return {s.id for s, shot in pairs if _undeliverable_reason(shot, rows) is not None}


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Calibration:
    """What one evaluation of a volume's shots gives its manifest (evaluate)."""
    status: str                                     # calibrationStatus
    entries: List[dict] = field(default_factory=list)  # the `calibration` entries
    waiting: int = 0                                # objects of carried shots not yet verified offsite
    undeliverable: Dict[int, str] = field(default_factory=dict)  # set id -> file_missing | upload_refused


def _undeliverable_reason(shot, rows) -> Optional[str]:
    """`file_missing` or `upload_refused` for a shot that cannot reach Fisqua, else None.

    Its files on disk are its master and any preview; an upload refused is a
    refusal of either object, for fixity or because another object holds the
    key, neither of which is retried.
    """
    from app.core.paths import resolve_within_storage

    for raw in (shot.file_path, shot.preview_path):
        if raw:
            p = resolve_within_storage(raw)
            if p is None or not p.is_file():
                return "file_missing"
    for kind in ("shot", "preview"):
        row = rows.get((shot.id, kind))
        if row is not None and row.refused_at is not None:
            return "upload_refused"
    return None


def evaluate(db: Session, collection, tenant: Optional[str], pages: List[dict]) -> Calibration:
    """The volume's calibrationStatus and `calibration`, and what they still wait for, from one read of the setting and the sets.

    `pages` are the manifest's image entries, each with its image's id under
    `_image_id`. app.core.capture_manifest builds the manifest from this
    result and refuses to build it while `waiting` is above zero
    (CalibrationWaiting), which keeps the volume in the outbox: readiness
    and content come from the same evaluation. Each shot of a taken set
    that applies to a page (its set's window, _applies) is in one of three
    cases:

    1. deliverable and verified offsite (the shot and any preview): carried;
    2. deliverable and not yet verified: not carried, and counted in
       `waiting`, so the manifest is not sent without it;
    3. undeliverable (a file missing on disk, an upload refused): not
       carried and not waited for; its set is in `undeliverable` and the
       volume is `skipped`.

    A shot no page follows is neither carried nor waited for. With the
    unit's setting off, or in a testing volume, nothing is carried or
    waited for, and the volume is `skipped`.

    A limit of this version: a volume sent as `skipped` because a shot
    could not be delivered is not sent again by itself when the shot later
    becomes deliverable; the volume's next handover carries it. GET
    /collections/{id}/colour-card shows each taken set's `delivery`.
    """
    from app.core import config
    from app.core.capture_manifest import ROTATE_DEGREES

    if not enabled(db, collection):
        return Calibration(SKIPPED)
    completed = _completed(_sets(db, collection.id))
    by_id = {p["_image_id"]: p for p in sorted(pages, key=lambda p: p["_image_id"])}
    pairs = _taken_shots(db, collection.id)
    rows = _objects(db, [shot.id for _s, shot in pairs])
    prefix = f"{tenant}/" if tenant else ""
    result = Calibration(NONE)
    for card_set, shot in pairs:
        reason = _undeliverable_reason(shot, rows)
        if reason is not None:
            result.undeliverable.setdefault(card_set.id, reason)
        first = _applies(card_set, completed, list(by_id))
        if first is None or reason is not None:
            continue
        obj, preview = rows.get((shot.id, "shot")), rows.get((shot.id, "preview"))
        unverified = (0 if _verified(obj) else 1) + (1 if shot.preview_path and not _verified(preview) else 0)
        if unverified:
            result.waiting += unverified
            continue
        entry = {
            "captureId": shot.capture_id,
            "filename": shot.filename,
            "objectKey": obj.object_key[len(prefix):] if obj.object_key.startswith(prefix) else obj.object_key,
            "sha256": shot.sha256,
        }
        if shot.preview_path:
            key = preview.object_key
            entry["previewObjectKey"] = key[len(prefix):] if key.startswith(prefix) else key
            entry["previewSha256"] = shot.preview_sha256
        if config.settings.FISQUA_ROTATE_DEG and shot.rotate_deg in ROTATE_DEGREES:
            entry["rotateDeg"] = int(shot.rotate_deg)
        # `camera` is the camera's name in the file names (`_c<n>`); the body
        # that took the shot goes with its settings, which Fisqua stores as given.
        entry.update({
            "camera": f"c{shot.camera_index}",
            "capturedAt": _iso(shot.captured_at),
            "settings": {**(shot.settings or {}), "body": {"hardwareId": shot.hardware_id, "role": shot.role,
                                                       "cameraIndex": shot.camera_index}},
            "appliesFromCaptureId": by_id[first]["captureId"],
        })
        result.entries.append(entry)
    page_ids = sorted(by_id)
    status_now = _status_of(completed, page_ids[0] if page_ids else None, set(result.undeliverable))
    result.status = PRESENT if status_now == PRESENT else SKIPPED
    return result


MAX_NAME_BYTES = 200


def name_problem(name) -> Optional[str]:
    """Why `name` is not a shot's file name under contract 1.12 §13 (one path segment of at most 200 bytes), or None."""
    if not isinstance(name, str) or name in ("", ".", ".."):
        return "empty, . or .."
    if "/" in name or "\\" in name:
        return "holds / or \\"
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        return "holds a control character"
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        return "holds an unpaired surrogate"
    if size > MAX_NAME_BYTES:
        return f"is {size} bytes long"
    return None


def manifest_problems(stem: str, shots: List[dict], pages: List[dict]) -> Tuple[List[str], List[str]]:
    """(key problems, identity problems) of the volume's `calibration` entries; both empty when the manifest may be written."""
    keys: List[str] = []
    identity: List[str] = []
    for shot in shots:
        name = shot.get("filename")
        why = name_problem(name)
        if why is not None:
            keys.append(f"{name!r} {why}")
            continue
        preview_name = f"{Path(name).stem}_preview.jpg"
        why = name_problem(preview_name)
        if why is not None:
            keys.append(f"{preview_name!r} {why}")
        if shot.get("objectKey") != f"{stem}/calibration/{name}":
            keys.append(f"{shot.get('objectKey')} is not {stem}/calibration/{name}")
        if "previewObjectKey" in shot and shot["previewObjectKey"] != f"{stem}/calibration/{preview_name}":
            keys.append(f"{shot['previewObjectKey']} is not {stem}/calibration/{preview_name}")
    seen: Dict[Tuple[str, str], str] = {}
    for page in pages:
        for field in ("captureId", "objectKey", "previewObjectKey"):
            if page.get(field):
                seen[("id" if field == "captureId" else "key", page[field])] = "a page"
    for shot in shots:
        for field in ("captureId", "objectKey", "previewObjectKey"):
            value = shot.get(field)
            if not value:
                continue
            slot = ("id" if field == "captureId" else "key", value)
            if slot in seen:
                identity.append(f"{field} {value} is also {seen[slot]}'s")
            else:
                seen[slot] = "another shot"
    return keys, identity


def uploadable_counts(db: Session, collection_ids) -> Dict[int, int]:
    """Per volume, how many objects its taken shots have to copy offsite (each shot, and a RAW shot's preview)."""
    from app.models.colour_card import ColourCardSet, ColourCardShot

    ids = list(collection_ids)
    out: Dict[int, int] = {}
    if not ids:
        return out
    for cid, preview in (db.query(ColourCardSet.collection_id, ColourCardShot.preview_path)
                         .join(ColourCardShot, ColourCardShot.set_id == ColourCardSet.id)
                         .filter(ColourCardSet.collection_id.in_(ids), ColourCardSet.outcome == TAKEN)):
        out[cid] = out.get(cid, 0) + (2 if preview else 1)
    return out
