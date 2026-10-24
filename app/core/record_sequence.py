"""Renumbering a volume: its records' `sequence`, and nothing on disk.

Exchange contract 1.1 §3: "Renumbering rewrites records' `sequence`; it never
renames a file." A file's name carries its capture ordinal, and order is the
records' `sequence`, which Fisqua reads to order pages. Fisqua's registration
refuses a volume in which two records share a sequence.
"""
from sqlalchemy.orm import Session

from app.models.record import Record


def renumber_volume_records(db: Session, collection_id: int) -> int:
    """Set the sequence of every record in the volume to 1..N; return N.

    The order kept is the one the records are listed and exported in:
    `sequence` with NULLs last, then the record id, so records that share a
    sequence keep the order of their ids and records without one go to the
    end. Only records whose `collection_id` is the volume's are read and
    written. The rows are read with SELECT ... FOR UPDATE (no lock clause on
    SQLite), so a concurrent write to one of them waits for the caller's
    commit. Nothing is committed here and no file is read or written: the
    caller commits, and a rollback leaves every sequence as it was.
    """
    records = (
        db.query(Record)
        .filter(Record.collection_id == collection_id)
        .order_by(Record.sequence.nulls_last(), Record.id)
        .with_for_update()
        .all()
    )
    for position, record in enumerate(records, start=1):
        if record.sequence != position:
            record.sequence = position
    return len(records)
