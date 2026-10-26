"""Records' `sequence` within a volume: renumbering, reordering, and the next free values.

Exchange contract 1.1 §3: "Renumbering rewrites records' `sequence`; it never
renames a file." A file's name carries its capture ordinal, and order is the
records' `sequence`, which Fisqua reads to order pages. Fisqua's registration
refuses a volume in which two records share a sequence, so every path that
puts a record into a volume or rewrites the order in one keeps the volume's
sequences distinct.

Two locks keep them distinct. A path that rewrites a volume's order locks the
volume's records and then the volume's row. A path that adds records to a
volume locks the volume's row and only then reads the highest sequence in it.
Each holds its locks until it commits, so an addition reads the maximum
either before a rewrite holds the volume's row (and the rewrite, which checks
the volume's records again once it holds the row, starts over to include
them) or after the rewrite has committed. Every path that
takes both a record's row and its volume's takes the record's first
(app/core/volume_stem.py), and a path that locks several records takes
them in id order. On SQLite, which the test suite runs on, no lock clause is
emitted.

Version: 0.1.0-beta
"""
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.volume_stem import locked_volume_query
from app.models.collection import Collection
from app.models.record import Record

REORDER_IDS_MESSAGE = "ordered_ids must name every record of the volume exactly once."


class ReorderIdsError(ValueError):
    """`ordered_ids` does not name every record of the volume exactly once."""


VOLUME_CHANGED_MESSAGE = "The volume changed while it was being renumbered. Try again."
LOCK_ATTEMPTS = 5


class VolumeChangedError(RuntimeError):
    """Records kept being added to the volume while its records were being locked."""


def listing_order(records: List[Record]) -> List[Record]:
    """`records` sorted as they are listed and exported: sequence (NULLs last), then id."""
    return sorted(records, key=lambda r: (r.sequence is None, r.sequence or 0, r.id))


def _records_in_order(db: Session, collection_id: int) -> List[Record]:
    """The volume's records, each row locked, in listing order.

    The rows are locked in id order, as every path that locks several
    records does (promotion, take-back, bulk status, move-records): two such
    paths over the same rows then wait on each other instead of deadlocking.
    They are sorted into listing order after they are read.
    """
    return listing_order(
        db.query(Record)
        .filter(Record.collection_id == collection_id)
        .order_by(Record.id)
        .with_for_update(of=Record)
        .populate_existing()
        .all()
    )


def _record_ids(db: Session, collection_id: int):
    """The query for the ids of the volume's records, read without a lock."""
    return db.query(Record.id).filter(Record.collection_id == collection_id)


def lock_volume_records(db: Session, collection_id: int) -> Tuple[Optional[Collection], List[Record]]:
    """Lock the volume's records, then its row; return the volume and its records in listing order.

    No record is locked once the volume's row is held: a retake holds its
    record and then waits for the volume, so a record lock taken after the
    volume's could close a cycle with it. Instead the ids of the volume's
    records are read again without a lock once the row is held. A record
    added to the volume and committed between the record locks and the
    volume lock makes the two sets differ; the transaction is then rolled
    back, releasing every lock, and the attempt starts over from the record
    locks. Once the row is held no record can be added, because every path
    that adds one takes the same row first. After LOCK_ATTEMPTS attempts that
    each found the set changed, raises VolumeChangedError. Returns (None, [])
    when no volume has the id.
    """
    for _ in range(LOCK_ATTEMPTS):
        records = _records_in_order(db, collection_id)
        volume = locked_volume_query(db, collection_id).first()
        if volume is None:
            return None, []
        if {rid for (rid,) in _record_ids(db, collection_id)} == {r.id for r in records}:
            return volume, records
        db.rollback()
    raise VolumeChangedError(VOLUME_CHANGED_MESSAGE)


def _assign(records: List[Record]) -> None:
    for position, record in enumerate(records, start=1):
        if record.sequence != position:
            record.sequence = position


def renumber_volume_records(db: Session, collection_id: int) -> Optional[int]:
    """Set the sequence of every record in the volume to 1..N, keeping listing order; return N.

    Records that share a sequence keep the order of their ids, and records
    without one go to the end. Only the volume's records are written, and no
    file is read or written. Nothing is committed here: the caller commits,
    and a rollback leaves every sequence as it was. Returns None when no
    volume has the id. Raises VolumeChangedError as lock_volume_records does.
    """
    volume, records = lock_volume_records(db, collection_id)
    if volume is None:
        return None
    _assign(records)
    return len(records)


def reorder_volume_records(db: Session, collection_id: int, ordered_ids: List[int]) -> Optional[int]:
    """Set the volume's records to 1..N in the order `ordered_ids` gives; return N.

    Raises ReorderIdsError, before anything is written, unless `ordered_ids`
    names every record of the volume once and nothing else, and
    VolumeChangedError as lock_volume_records does. Nothing is committed
    here. Returns None when no volume has the id.
    """
    volume, records = lock_volume_records(db, collection_id)
    if volume is None:
        return None
    by_id = {r.id: r for r in records}
    if len(ordered_ids) != len(set(ordered_ids)) or set(ordered_ids) != set(by_id):
        raise ReorderIdsError(REORDER_IDS_MESSAGE)
    _assign([by_id[rid] for rid in ordered_ids])
    return len(records)


def next_sequences(db: Session, collection_id: int, count: int) -> Optional[List[int]]:
    """Lock the volume's row and return the `count` sequences after the highest in it.

    1, 2, ... for a volume with no sequenced record. The maximum is read after
    the row is held, and the row stays locked until the caller's transaction
    ends, so two additions to one volume, or an addition and a renumber,
    cannot hand out the same value. The caller writes the values and commits.
    A caller that also locks a record locks it before calling this. Returns
    None when no volume has the id.
    """
    if locked_volume_query(db, collection_id).first() is None:
        return None
    highest = (
        db.query(func.max(Record.sequence))
        .filter(Record.collection_id == collection_id)
        .scalar()
    )
    start = 1 if highest is None else highest + 1
    return list(range(start, start + count))
