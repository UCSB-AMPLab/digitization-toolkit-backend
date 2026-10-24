"""No capture outside a volume, and none into a volume without a filename_stem.

Exchange contract 1.1 §3 names every captured image
`<filename_stem>-img_<nnnn>_c<n>`, withdraws the timestamp names used when
there was no stem, and Captua never makes a stem up. So the capture
endpoints and the image upload refuse, with a 409, a capture into no volume
(a record attached straight to a project), carrying VOLUME_REQUIRED_DETAIL,
and a capture into a volume without a stem, carrying STEM_REQUIRED_DETAIL.
They refuse before any camera is triggered or any file is written. The
interface matches each 409 on its exact text, so the texts do not change.
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.models.collection import Collection


STEM_REQUIRED_DETAIL = "This volume has no file name stem. Give it one before capturing into it."
VOLUME_REQUIRED_DETAIL = "Captures must go into a volume."


def volume_lacks_stem(db: Session, collection_id: Optional[int]) -> bool:
    """True when collection_id names an existing volume whose filename_stem is NULL.

    False for None and for an id that matches no volume: the caller's own
    lookup answers a missing volume with its 404.
    """
    if collection_id is None:
        return False
    stem = db.query(Collection.filename_stem).filter(Collection.id == collection_id).first()
    return stem is not None and stem[0] is None
