"""Colour card shots of a volume (NEH-340, app.core.colour_card): the routes under /collections/{id}/colour-card.

Administrator and operator for the writes; every role that reads a
volume's images reads a shot's image. A testing volume answers as not
enabled. Refusals carry {code, message}.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, StrictInt
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker
from app.api.deps import get_db_dependency
from app.core import colour_card as cc
from app.core import unit_activity
from app.models.collection import Collection
from app.models.user import User

router = APIRouter()

allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])
_CAMERA_BUSY = [Depends(unit_activity.tracked)]


class SetRequest(BaseModel):
    """The body of POST .../sets and POST .../skip; both fields optional, and the body too.

    `capture_mode` says which cameras take a shot (`dual`: cameras 0 and 1;
    `single`: `camera_index`, default 0). Absent, it is the volume's latest
    record's mode, else `dual` when camera 1 is connected.
    """
    model_config = ConfigDict(extra="forbid")

    capture_mode: Optional[Literal["single", "dual"]] = None
    camera_index: Optional[StrictInt] = None


class ShotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera_index: StrictInt
    resolution: str = "high"


def _volume(db: Session, collection_id: int) -> Collection:
    volume = db.query(Collection).filter(Collection.id == collection_id).first()
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Collection {collection_id} not found")
    return volume


@router.get("/{collection_id}/colour-card")
def get_colour_card(
    collection_id: int,
    read_cameras: bool = Query(False, description="Read the cameras' current settings (no picture is taken)"),
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Whether the volume is asked for colour card shots now, its status and its sets.

    `{enabled, needed, reason, status, sets}`. `needed` is true with a
    `reason` (`start`, `begun_without`, `settings_changed`), false, or null
    when a camera's settings cannot be read. Without `read_cameras=1` no
    camera is read; a gphoto2 camera's exposure is then unknown.
    """
    return cc.volume_answer(db, _volume(db, collection_id), read_cameras=read_cameras)


@router.post("/{collection_id}/colour-card/sets", dependencies=_CAMERA_BUSY)
def open_colour_card_set(
    collection_id: int,
    body: Optional[SetRequest] = None,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Open a set (the volume's open set if there is one), recording the cameras' current settings."""
    body = body or SetRequest()
    card_set = cc.open_set(db, collection_id, current_user, body.capture_mode, body.camera_index)
    return cc.set_dict(card_set)


@router.post("/{collection_id}/colour-card/sets/{set_id}/shots", dependencies=_CAMERA_BUSY)
def take_colour_card_shot(
    collection_id: int,
    set_id: int,
    body: ShotRequest,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Photograph the colour card with one camera of an open set; a second shot replaces the first."""
    shot = cc.take_shot(db, collection_id, set_id, body.camera_index, current_user, body.resolution)
    return cc.shot_dict(shot, collection_id)


@router.post("/{collection_id}/colour-card/sets/{set_id}/complete")
def complete_colour_card_set(
    collection_id: int,
    set_id: int,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Mark the set taken; refused (409 colour_card_shots_missing) until every camera of its mode has a shot."""
    return cc.set_dict(cc.complete(db, collection_id, set_id, current_user))


@router.post("/{collection_id}/colour-card/skip", dependencies=_CAMERA_BUSY)
def skip_colour_card(
    collection_id: int,
    body: Optional[SetRequest] = None,
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Skip the request: the open set (its shots removed), or a new set recording the current settings."""
    body = body or SetRequest()
    return cc.set_dict(cc.skip(db, collection_id, current_user, body.capture_mode, body.camera_index))


@router.get("/{collection_id}/colour-card/shots/{shot_id}/preview")
def get_colour_card_shot_preview(
    collection_id: int,
    shot_id: int,
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """The shot as a browser shows it, inline: a JPEG master itself, a RAW master's `_preview.jpg`; 404 otherwise."""
    from app.core.paths import is_raw_suffix, resolve_within_storage

    shot = cc.shot_of(db, collection_id, shot_id)
    master = resolve_within_storage(shot.file_path)
    if master is not None and not is_raw_suffix(master) and master.suffix.lower() in (".jpg", ".jpeg"):
        path, name = master, shot.filename
    elif shot.preview_path:
        path, name = resolve_within_storage(shot.preview_path), shot.preview_filename
    else:
        path, name = None, None
    if path is None or not path.is_file():
        raise cc.refusal(404, cc.NO_PREVIEW, cc.NO_PREVIEW_MESSAGE)
    return FileResponse(path=path, filename=name, media_type="image/jpeg", content_disposition_type="inline")
