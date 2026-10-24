"""Raise a volume's capture_counter, once per capture event.

Exchange contract 1.1 makes `<nnnn>` in a volume's file names count every
capture the volume has ever had, superseded ones included, and never reuse a
number. collections.capture_counter is that count, and next_capture_number
is the one place a capture raises it. raise_capture_counter is the
operator's way to raise it past numbers taken outside this unit.
"""
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.core.volume_stem import locked_volume_query
from app.models.collection import Collection

CATEGORY = "system"
RAISE_ACTION = "capture_counter_raised"


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


def raise_capture_counter(db: Session, stem: str, n: int, *, apply: bool = False) -> dict:
    """Raise the counter of the volume whose filename_stem is `stem` to `n`.

    For numbers given outside this unit: images copied into the bucket under
    contract names from a rename map exist only there, and the map's
    counterAfter is the highest number it issued. Raised to that value, the
    counter makes the next capture take counterAfter + 1.

    The volume row is taken under locked_volume_query, as a capture takes it
    before it raises the counter, so the two wait on each other and neither
    reads a counter the other is about to change. Nothing here locks a
    record, so the rule that a record is locked before its volume holds.

    Raises ValueError for a stem no volume has, for `n` outside 0 to
    MAX_CAPTURE_NUMBER, and for `n` lower than the current counter, which
    never decreases. An `n` equal to the counter changes nothing. Without
    `apply` nothing is written and the lock is released at once; the report
    says what would be done. With it, the raise is committed and logged as
    RAISE_ACTION.
    """
    from app.core import audit
    from app.core.capture_naming import MAX_CAPTURE_NUMBER

    stem = (stem or "").strip()
    if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= MAX_CAPTURE_NUMBER:
        raise ValueError(f"the counter must be a whole number from 0 to {MAX_CAPTURE_NUMBER}, not {n!r}")
    collection_id = (db.query(Collection.id).filter(Collection.filename_stem == stem).scalar()
                     if stem else None)
    volume = locked_volume_query(db, collection_id).first() if collection_id is not None else None
    if volume is None or volume.filename_stem != stem:
        db.rollback()
        raise ValueError(f"no volume has the file name stem {stem!r}")
    db.refresh(volume)  # the value under the lock, not one this session read before it
    before = volume.capture_counter
    if n < before:
        db.rollback()
        raise ValueError(f"the capture counter of volume {stem!r} is already {before}; "
                         f"it never decreases, so it cannot be set to {n}")
    report = {"stem": stem, "collection_id": volume.id, "counter_before": before,
              "counter_after": n, "raised": n > before, "applied": apply}
    if not apply or n == before:
        db.rollback()
        return report
    volume.capture_counter = n
    db.commit()
    audit.log_event(db, level="INFO", category=CATEGORY, action=RAISE_ACTION, actor=None,
                    subject=stem, detail=f"collection_id={volume.id} from={before} to={n}")
    return report
