"""One row per object the unit puts in, or adopts from, the offsite bucket.

A table rather than columns on record_images, for three reasons. An image can
have two objects (its capture and, for a RAW master, its preview), each with
its own key, digest and timestamps, which as columns would be two parallel
sets of six. The object key has to be unique across every object, captures
and previews together, which one constraint on one column expresses and two
column sets cannot. And an object stays in the bucket when its image row is
deleted, so the row keeps its key (record_image_id is set to NULL) as the
unit's record of what it put there.

States, all derived from the columns:

- verified: `verified_at` is set. A listing taken after the upload, or the
  adoption listing, showed `object_key` with `local_sha1`.
- refused: `refused_at` is set, and `refusal` says why. `remote_differs`:
  before the upload, the listing showed an object at `object_key` with
  another SHA1 (`remote_sha1`). `fixity`: the file's sha256 is not the one
  its capture record holds. Nothing is uploaded, and the uploader does not
  retry it.
- pending: neither. `uploaded_at` set means the copy command succeeded and
  no listing has yet confirmed it.

An adopted object (app.core.image_upload.adopt_objects) has `verified_at`
and no `uploaded_at`: this unit's uploader never sent it.

`object_key` is relative to OFFSITE_REMOTE. It is written when the row is
created and never changed (OffsiteObject's before_update guard).
"""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import relationship

from app.core.db import Base
from app.models.record import ImmutableFieldError

OBJECT_KINDS = ("capture", "preview")
REFUSALS = ("remote_differs", "fixity")


class OffsiteObject(Base):
    __tablename__ = "offsite_objects"

    id = Column(Integer, primary_key=True, index=True)
    record_image_id = Column(Integer, ForeignKey("record_images.id", ondelete="SET NULL"), nullable=True, index=True)
    kind = Column(String(10), nullable=False)
    object_key = Column(String(1024), nullable=False)
    local_sha1 = Column(String(40), nullable=False)
    remote_sha1 = Column(String(40), nullable=True)
    uploaded_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    refused_at = Column(DateTime, nullable=True)
    refusal = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    image = relationship("RecordImage")

    __table_args__ = (
        UniqueConstraint("object_key", name="offsite_objects_object_key_key"),
        UniqueConstraint("record_image_id", "kind", name="offsite_objects_image_kind_key"),
        CheckConstraint("kind IN ('capture', 'preview')", name="check_offsite_object_kind"),
        CheckConstraint("verified_at IS NULL OR refused_at IS NULL", name="check_offsite_object_state"),
        CheckConstraint(
            "(refused_at IS NULL AND refusal IS NULL) OR "
            "(refused_at IS NOT NULL AND refusal IS NOT NULL AND refusal IN ('remote_differs', 'fixity'))",
            name="check_offsite_object_refusal"),
    )


@event.listens_for(OffsiteObject, "before_update")
def _object_key_is_frozen(mapper, connection, target):
    from sqlalchemy import inspect

    history = inspect(target).attrs.object_key.history
    if history.deleted and history.deleted[0] != target.object_key:
        raise ImmutableFieldError(
            f"offsite object {target.id}: object_key is fixed at first upload"
        )
