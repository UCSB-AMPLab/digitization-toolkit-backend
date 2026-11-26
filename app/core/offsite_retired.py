"""Retired offsite keys: the objects an adoption leaves in the bucket under an image's old key (NEH-387).

An adoption (app.core.adopt_captures) renames each source file under the
target's stem, and the ordinary upload pass then uploads it again under its
new key. The unit's bucket key can upload and list but cannot read, copy or
delete, so the object at the old key stays in the bucket until a person
deletes it.

retire_rows prepares an image for that: each of its offsite_objects rows,
all of which must be verified, is detached (record_image_id NULL) and marked
retired (retired_at, retired_image_id), in the caller's transaction. The
row keeps its key, its SHA1 and its timestamps, and `object_key` still never
changes (OffsiteObject's before_update guard). Detached, the row is read
by nothing that joins rows to images by record_image_id: the upload's work
list, the received indicator, the free job, the capture manifest and the
way back into a volume (image_upload.offsite_keys_under_volume) see the
image as having no object, so it is pending again and uploaded under the
key its current file name and volume stem give. The stored-offsite check
(image_upload.records_stored_offsite) also reads retired_image_id, so the
record stays in its volume while its bytes are offsite only under the
retired key. Read by key, it is still held: the old stem stays reserved
(app.models.collection.offsite_keys_under_stem), no new upload or adoption
takes its key (image_upload._key_holder, adopt_objects' held keys,
adopt_rename_map), and the old volume counts it as copied
(kept_volumes).

retire_card_rows does the same for a colour card shot adopted with its
volume: each of its colour_card_objects rows, all verified, is detached
(shot_id NULL) and marked retired (retired_at, retired_shot_id). The upload's
work list for shots (image_upload.colour_card_work) then finds the shot with
no row of that kind and uploads it under the key its new name and volume
give; read by key, the old row still holds its key and its stem.

retired_keys lists the retired rows of both tables for the person who
deletes the old objects, in one list. A shot's rows carry the kinds
`colour_card_shot` and `colour_card_preview`, so they are told apart from a
page's `capture` and `preview`. A key is `deletable` only while the image or shot it came from
has a current row of the same kind that is verified and carries the same
local SHA1: the bytes are then recorded at the new key. Nothing here lists or
changes the bucket.

Version: 0.3.3
"""

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session, aliased


class RetireRefused(ValueError):
    """An image to be retired has a row that is not verified; nothing was changed.

    `unverified` lists each such row as {image_id, kind, key, state}, where
    state is "pending" (no copy yet), "uploaded" (copied, not yet seen by a
    listing) or "refused".
    """

    def __init__(self, unverified: list):
        self.unverified = unverified
        names = ", ".join(f"image {u['image_id']} {u['kind']} ({u['state']})" for u in unverified)
        super().__init__(f"only verified offsite objects can be retired, and these are not: {names}")


def _state(row) -> str:
    if row.verified_at is not None:
        return "verified"
    if row.refused_at is not None:
        return "refused"
    if row.uploaded_at is not None:
        return "uploaded"
    return "pending"


def retire_rows(db: Session, image_ids: Iterable[int], *, now: datetime) -> list:
    """Detach and retire every offsite_objects row of the images, and return the rows.

    Every row of every image (capture and preview) must be verified; any
    other raises RetireRefused before anything is changed. The rows are
    locked FOR UPDATE, then each gets record_image_id NULL, retired_at `now`
    and retired_image_id the image's id, and the session is flushed. Nothing
    is committed: the caller's transaction decides. An image with no row
    retires nothing.
    """
    from app.models.offsite_object import OffsiteObject

    ids = sorted(set(image_ids))
    rows = []
    for start in range(0, len(ids), 500):
        rows.extend(db.query(OffsiteObject)
                    .filter(OffsiteObject.record_image_id.in_(ids[start:start + 500]))
                    .order_by(OffsiteObject.record_image_id, OffsiteObject.kind)
                    .with_for_update()
                    .all())
    unverified = [{"image_id": r.record_image_id, "kind": r.kind, "key": r.object_key, "state": _state(r)}
                  for r in rows if r.verified_at is None]
    if unverified:
        raise RetireRefused(unverified)
    for row in rows:
        row.retired_image_id = row.record_image_id
        row.record_image_id = None
        row.retired_at = now
    db.flush()
    return rows


class CardRetireRefused(ValueError):
    """A shot to be retired has a row that is not verified; nothing was changed.

    `unverified` lists each such row as {shot_id, kind, key, state}, the
    states of RetireRefused.
    """

    def __init__(self, unverified: list):
        self.unverified = unverified
        names = ", ".join(f"colour card shot {u['shot_id']} {u['kind']} ({u['state']})" for u in unverified)
        super().__init__(f"only verified offsite objects can be retired, and these are not: {names}")


def retire_card_rows(db: Session, shot_ids: Iterable[int], *, now: datetime) -> list:
    """Detach and retire every colour_card_objects row of the shots, and return the rows (as retire_rows).

    Every row must be verified; any other raises CardRetireRefused before
    anything is changed. The rows are locked FOR UPDATE, then each gets
    shot_id NULL, retired_at `now` and retired_shot_id the shot's id, and
    the session is flushed. Nothing is committed. A shot with no row
    retires nothing.
    """
    from app.models.colour_card import ColourCardObject

    ids = sorted(set(shot_ids))
    rows = []
    for start in range(0, len(ids), 500):
        rows.extend(db.query(ColourCardObject)
                    .filter(ColourCardObject.shot_id.in_(ids[start:start + 500]))
                    .order_by(ColourCardObject.shot_id, ColourCardObject.kind)
                    .with_for_update()
                    .all())
    unverified = [{"shot_id": r.shot_id, "kind": r.kind, "key": r.object_key, "state": _state(r)}
                  for r in rows if r.verified_at is None]
    if unverified:
        raise CardRetireRefused(unverified)
    for row in rows:
        row.retired_shot_id = row.shot_id
        row.shot_id = None
        row.retired_at = now
    db.flush()
    return rows


CARD_KINDS = {"shot": "colour_card_shot", "preview": "colour_card_preview"}


def retired_keys(db: Session) -> list:
    """Every retired row, oldest retirement first, as the person deleting old objects needs it.

    Each entry: `key`, `kind`, `local_sha1` and `remote_sha1` as stored,
    `bytes` (always None: offsite_objects stores no size), `verified_at`,
    `retired_at`, `image_id` (the image it came from, None once that image
    is deleted) or `shot_id` (the colour card shot, likewise; the other is
    None), `volume_id` and `volume_stem` (the volume that image or shot is
    in now, None when it is in none), `current_key` (its current row of the
    same kind, or None) and `deletable`. A page's kinds are `capture` and
    `preview`; a shot's are `colour_card_shot` and `colour_card_preview`.
    Pages' and shots' keys are in one list, by retirement time.
    """
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    current = aliased(OffsiteObject)
    rows = (db.query(OffsiteObject, current, Collection.id, Collection.filename_stem)
            .outerjoin(current, (current.record_image_id == OffsiteObject.retired_image_id)
                       & (current.kind == OffsiteObject.kind))
            .outerjoin(RecordImage, RecordImage.id == OffsiteObject.retired_image_id)
            .outerjoin(Record, Record.id == RecordImage.record_id)
            .outerjoin(Collection, Collection.id == Record.collection_id)
            .filter(OffsiteObject.retired_at.isnot(None))
            .order_by(OffsiteObject.retired_at, OffsiteObject.id)
            .all())
    out = [(row.retired_at, 0, row.id,
            {**_entry(row, now_row), "shot_id": None, "volume_id": volume_id, "volume_stem": stem})
           for row, now_row, volume_id, stem in rows]
    out.extend(_card_keys(db))
    return [entry for *_order, entry in sorted(out, key=lambda e: e[:3])]


def _card_keys(db: Session) -> list:
    """(retired_at, 1, id, entry) for every retired colour_card_objects row."""
    from app.models.collection import Collection
    from app.models.colour_card import ColourCardObject, ColourCardSet, ColourCardShot

    current = aliased(ColourCardObject)
    rows = (db.query(ColourCardObject, current, Collection.id, Collection.filename_stem)
            .outerjoin(current, (current.shot_id == ColourCardObject.retired_shot_id)
                       & (current.kind == ColourCardObject.kind))
            .outerjoin(ColourCardShot, ColourCardShot.id == ColourCardObject.retired_shot_id)
            .outerjoin(ColourCardSet, ColourCardSet.id == ColourCardShot.set_id)
            .outerjoin(Collection, Collection.id == ColourCardSet.collection_id)
            .filter(ColourCardObject.retired_at.isnot(None))
            .order_by(ColourCardObject.retired_at, ColourCardObject.id)
            .all())
    out = []
    for row, now_row, volume_id, stem in rows:
        deletable = (row.retired_shot_id is not None and now_row is not None and now_row.verified_at is not None
                     and now_row.local_sha1 == row.local_sha1)
        out.append((row.retired_at, 1, row.id, {
            "key": row.object_key,
            "kind": CARD_KINDS.get(row.kind, row.kind),
            "local_sha1": row.local_sha1,
            "remote_sha1": row.remote_sha1,
            "bytes": None,
            "verified_at": row.verified_at,
            "retired_at": row.retired_at,
            "image_id": None,
            "shot_id": row.retired_shot_id,
            "current_key": now_row.object_key if now_row is not None else None,
            "deletable": bool(deletable),
            "volume_id": volume_id,
            "volume_stem": stem,
        }))
    return out


def _entry(row, current: Optional[object]) -> dict:
    deletable = (row.retired_image_id is not None and current is not None
                 and current.verified_at is not None
                 and current.local_sha1 == row.local_sha1)
    return {
        "key": row.object_key,
        "kind": row.kind,
        "local_sha1": row.local_sha1,
        "remote_sha1": row.remote_sha1,
        "bytes": None,
        "verified_at": row.verified_at,
        "retired_at": row.retired_at,
        "image_id": row.retired_image_id,
        "current_key": current.object_key if current is not None else None,
        "deletable": bool(deletable),
    }
