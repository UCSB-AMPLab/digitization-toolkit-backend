"""One place that refuses a write to a collection an export is bagging.

A bag is a statement about a collection at an instant. Its metadata.json names
the collection's title, description, type and archival metadata, its project,
and for every record the sequence, title, description, author, date, status and
capture mode, with the filenames and dimensions of its current images. Change
any of those while the archive is being assembled and the bag is a statement
about nothing in particular -- a review that no longer stands, a page order
that was replaced, a filename that now belongs to a different image.

The first attempt compared a snapshot of (status, current image ids) taken at
the gate against the same tuple taken once the payload was planned, and refused
to write the bag if they differed. That covered two of the eleven fields the
metadata asserts, and it covered them by throwing away the whole assembly after
the integrity checks had already run. The ruling of 2026-10-20 is to serialise
instead: while an export holds a collection, the collection does not change.

Sixteen routes across app/api/collections.py, app/api/records.py and
app/api/projects.py can change something a bag asserts. This is deliberately
NOT sixteen checks. The last round of this work defeated a sixteen-places rule
in two calls, through the two places nobody had edited -- the story is in
assert_movable in app/api/records.py, and the lesson there was that the
enumeration matters more than the check. So the rule lives in two forms:

  * a before_flush listener, registered on SessionLocal in app/core/db.py, so
    no write to a Record, RecordImage or Collection can reach the database
    without passing it -- including from routes that do not exist yet;

  * assert_collection_writable(), called early by the handful of routes that
    change the filesystem BEFORE they commit, where a refusal at flush time
    would arrive after the files were already gone.

Annotations are the one mutation deliberately left open: nothing in the bag
reads them.
"""

import logging

from fastapi import HTTPException
from sqlalchemy.orm import attributes

from app.core.export_jobs import export_jobs

logger = logging.getLogger(__name__)


def _ids_from_history(obj, field: str) -> set[int]:
    """Every value `field` has held on this object, old and new.

    Both ends matter for a move: taking a record out of a collection an export
    is bagging changes that bag, and putting one into another export's
    collection changes that one.
    """
    history = attributes.get_history(obj, field)
    return {
        value
        for value in (*history.added, *history.deleted, *history.unchanged)
        if value is not None
    }


def collection_ids_pending(session) -> set[int]:
    """Collections this session is about to write to.

    Resolved per object rather than per route, which is what makes the listener
    independent of who calls it. Images are resolved through their record,
    because that is where the collection is recorded.
    """
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage

    collection_ids: set[int] = set()
    record_ids: set[int] = set()

    for obj in (*session.new, *session.deleted, *session.dirty):
        if isinstance(obj, Record):
            collection_ids |= _ids_from_history(obj, "collection_id")
        elif isinstance(obj, RecordImage):
            record_ids |= _ids_from_history(obj, "record_id")
        elif isinstance(obj, Collection):
            # A new collection has no id yet, so nothing can be exporting it.
            # Only this collection: an export bags the records filtered by
            # collection_id, so a sub-collection's own rows are not in the bag.
            if obj.id is not None:
                collection_ids.add(obj.id)

    if record_ids:
        # Identity-map first, and SQL only for what is not already loaded.
        # Safe inside before_flush because SessionLocal is autoflush=False.
        for (collection_id,) in session.query(Record.collection_id).filter(
            Record.id.in_(record_ids)
        ):
            if collection_id is not None:
                collection_ids.add(collection_id)

    return collection_ids


def exporting_holds(collection_ids) -> list[int]:
    """Which of these collections an export is currently bagging."""
    return sorted(
        cid for cid in collection_ids if export_jobs.active_for(cid) is not None
    )


def _refuse(held: list[int]) -> None:
    plural = "s" if len(held) > 1 else ""
    raise HTTPException(
        status_code=409,
        detail=(
            f"Collection{plural} {held} {'are' if plural else 'is'} being exported. "
            f"An archive describes the collection as it was when the export "
            f"started, so nothing in it can change until the export finishes or "
            f"fails. Wait for it, then try again."
        ),
    )


def assert_collection_writable(collection_id) -> None:
    """Refuse now, before the caller touches the filesystem.

    For the routes that delete files, rename files or write an upload before
    they commit. The listener would refuse them too, but only at flush time,
    with the files already changed and nothing in the database to say so.
    """
    if collection_id is None:
        return
    held = exporting_holds({collection_id})
    if held:
        _refuse(held)


def assert_no_export_holds(session) -> None:
    """The listener's body: refuse the flush if it would change a bag."""
    held = exporting_holds(collection_ids_pending(session))
    if held:
        logger.info(
            "Refused a write to collection(s) %s: an export holds them", held
        )
        _refuse(held)
