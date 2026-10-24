"""No capture outside a volume, and none into a volume without a filename_stem.

Exchange contract 1.1 §3 names every captured image
`<filename_stem>-img_<nnnn>_c<n>`, withdraws the timestamp names used when
there was no stem, and Captua never makes a stem up. So the capture
endpoints and the image upload refuse, with a 409, a capture into no volume
(a record attached straight to a project), carrying VOLUME_REQUIRED_DETAIL,
and a capture into a volume without a stem, carrying STEM_REQUIRED_DETAIL.
The capture endpoints also refuse a retake that names a volume other than
its record's, carrying RETAKE_VOLUME_DETAIL, and a retake of a record that
is neither captured nor rejected, carrying RETAKE_STATUS_DETAIL.
They refuse before any camera is triggered or any file is written. The
interface matches each 409 on its exact text, so the texts do not change.
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.models.collection import Collection


STEM_REQUIRED_DETAIL = "This volume has no file name stem. Give it one before capturing into it."
VOLUME_REQUIRED_DETAIL = "Captures must go into a volume."
RETAKE_VOLUME_DETAIL = "A retake must go into the record's own volume."
RETAKE_STATUS_DETAIL = "Only a captured or rejected record can be retaken."


def locked_volume_query(db: Session, collection_id: int):
    """The volume row under SELECT ... FOR UPDATE (no lock clause on SQLite).

    The stem check reads the row this way so that the lock is held until the
    capture's transaction ends. A PATCH clearing or setting the stem takes the
    same row the same way (app/api/collections.py), so it cannot land between
    the check and the commit. A capture holds the lock through the shot.

    A path that also locks a record locks the record first and then this
    row; the opposite order deadlocks against one that keeps to it.
    """
    return db.query(Collection).filter(Collection.id == collection_id).with_for_update()


def volume_lacks_stem(db: Session, collection_id: Optional[int]) -> bool:
    """True when collection_id names an existing volume whose filename_stem is NULL.

    Locks the volume row (locked_volume_query) for the rest of the
    transaction. False for None and for an id that matches no volume: the
    caller's own lookup answers a missing volume with its 404.
    """
    if collection_id is None:
        return False
    volume = locked_volume_query(db, collection_id).first()
    return volume is not None and volume.filename_stem is None
