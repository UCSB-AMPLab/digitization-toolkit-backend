"""The captures a client named with its own id, and what each one saved.

A capture request may carry `client_capture_id`, a UUID the client mints
for one capture. When the capture is saved, a row here is written in the
same transaction as its record and images, so a row exists exactly when
the capture was saved. A repeated request with the same id is answered
from the row and never reaches the camera (app.core.capture_requests).

- `client_capture_id`: the client's id, in canonical lowercase form.
- `space`: `real`, or `testing` for the testing account's captures
  (`check_capture_request_space`), written from the caller when the row is
  stored. Ids are each space's own: one row per id and space
  (`capture_requests_client_capture_id_space_key`), and every read selects
  by the row's own space, never through its record or volume, so a record
  that a purge leaves without a volume does not move its row into real work.
- `kind`: `single` or `dual`, the route that saved it
  (`check_capture_request_kind`).
- `collection_id`: the volume the request named; set null if the volume is
  deleted, as a record's is.
- `record_id`: the record the capture created or retook; set null if the
  record is deleted. The row outlives its record, so a repeated request is
  refused (`capture_no_longer_stored`), never photographed again. One
  record may hold several rows: a retake, a placeholder fill and an insert
  each carry their own id. Rows are kept; nothing removes them.
- `image_ids`: the ids of the images the capture saved, in the order the
  capture answered them. They stay the capture's answer after a later
  retake supersedes them.
- `request_fingerprint`: SHA-256, in hex, of the request that saved the
  capture (app.core.capture_requests.fingerprint). A request with the id
  and another fingerprint is not answered from the row.
- `created_at`: when the capture was saved.

This id is the client's and is not `RecordImage.capture_id`, which the
server mints for the manifest.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.core.db import Base

KINDS = ("single", "dual")
KIND_CHECK = "kind IN ('single', 'dual')"
SPACES = ("real", "testing")
SPACE_CHECK = "space IN ('real', 'testing')"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class StoredCaptureRequest(Base):
    __tablename__ = "capture_requests"

    id = Column(Integer, primary_key=True)
    client_capture_id = Column(String(36), nullable=False)
    space = Column(String(10), nullable=False)
    kind = Column(String(10), nullable=False)
    collection_id = Column(
        Integer, ForeignKey("collections.id", ondelete="SET NULL", name="capture_requests_collection_id_fkey"),
        nullable=True)
    record_id = Column(
        Integer, ForeignKey("records.id", ondelete="SET NULL", name="capture_requests_record_id_fkey"),
        nullable=True, index=True)
    image_ids = Column(JSON, nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_capture_id", "space", name="capture_requests_client_capture_id_space_key"),
        CheckConstraint(KIND_CHECK, name="check_capture_request_kind"),
        CheckConstraint(SPACE_CHECK, name="check_capture_request_space"),
    )
