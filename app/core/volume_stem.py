"""No capture into a volume that has no filename_stem.

Exchange contract 1.1 §3 names every captured image
`<filename_stem>-img_<nnnn>_c<n>`, and Captua never makes a stem up, so a
volume without one cannot take a capture. The capture endpoints and the
image upload refuse it with a 409 carrying STEM_REQUIRED_DETAIL, before any
camera is triggered or any file is written. The interface matches the 409 on
this exact text, so it does not change.

A capture into no volume (a record attached to a project directly) is not
refused here.
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.models.collection import Collection


STEM_REQUIRED_DETAIL = "This volume has no file name stem. Give it one before capturing into it."


def volume_lacks_stem(db: Session, collection_id: Optional[int]) -> bool:
    """True when collection_id names an existing volume whose filename_stem is NULL.

    False for None and for an id that matches no volume: the caller's own
    lookup answers a missing volume with its 404.
    """
    if collection_id is None:
        return False
    stem = db.query(Collection.filename_stem).filter(Collection.id == collection_id).first()
    return stem is not None and stem[0] is None
