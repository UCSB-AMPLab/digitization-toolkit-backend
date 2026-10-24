"""Raise a volume's capture_counter, once per capture event.

Exchange contract 1.1 makes `<nnnn>` in a volume's file names count every
capture the volume has ever had, superseded ones included, and never reuse a
number. collections.capture_counter is that count, and next_capture_number
is the one place it is raised.
"""
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.models.collection import Collection


def next_capture_number(db: Session, collection_id: Optional[int]) -> Optional[int]:
    """Raise the volume's capture_counter by one and return the new value.

    Call it once per capture event, in the transaction that writes the
    event's images, so that a rollback undoes the raise with them. The value
    returned is the number the event's file names will carry.

    One atomic statement, `UPDATE ... SET capture_counter = capture_counter
    + 1 ... RETURNING capture_counter`, so the database reads and writes the
    counter together: on Postgres a second capture into the same volume
    waits on the row the first has updated and then raises the committed
    value, so no increment is lost and no number is handed out twice. The
    row stays locked until the transaction ends.

    The statement goes around the ORM, so a Collection already loaded in
    this session is given the new value as its committed state; otherwise
    has_captured() and the never-decreases guard would read the old one.

    Returns None, raising nothing, when the capture is in no volume (a
    record attached to a project directly), or when the volume does not
    exist.
    """
    if collection_id is None:
        return None
    table = Collection.__table__
    value = db.execute(
        update(table)
        .where(table.c.id == collection_id)
        .values(capture_counter=table.c.capture_counter + 1)
        .returning(table.c.capture_counter)
    ).scalar_one_or_none()
    if value is not None:
        loaded = db.identity_map.get(identity_key(Collection, collection_id))
        if loaded is not None:
            set_committed_value(loaded, "capture_counter", value)
    return value
