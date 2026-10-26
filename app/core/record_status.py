"""Writing a record's QA status together with its history row.

Every change of records.status goes through change_record_status, so the
status and the row in record_status_changes that dates it are written in the
same transaction. Callers hold the record row lock (see
app.api.records._locked_record), so the old status the row names is the one
the change replaced. The one exception is take-back, which moves a whole
collection in a single UPDATE and adds its rows with
record_bulk_status_changes. A record's birth as "captured" is not a change and
writes no row; its created_at dates it.

Version: 0.1.0-beta
"""
from typing import Iterable

from sqlalchemy.orm import Session

from app.models.record import Record, RecordStatusChange


def change_record_status(db: Session, rec: Record, new_status: str, actor: str | None) -> None:
	"""Set rec.status to new_status and add the history row; no-op if unchanged.

	The caller validates the transition and commits. The row is added by
	record_id rather than through rec.status_changes, so a promotion of 825
	records does not load each record's history to append to it.
	"""
	if rec.status == new_status:
		return
	db.add(RecordStatusChange(
		record_id=rec.id,
		collection_id=rec.collection_id,
		from_status=rec.status,
		to_status=new_status,
		changed_by=actor,
	))
	rec.status = new_status


def record_bulk_status_changes(
	db: Session,
	record_ids: Iterable[int],
	collection_id: int | None,
	from_status: str,
	to_status: str,
	actor: str | None,
) -> None:
	"""Add history rows for records a bulk UPDATE has already moved.

	Pass the ids the UPDATE matched (its RETURNING list). Take-back locks the
	rows before its UPDATE, so there the two lists are the same; the RETURNING
	list stays correct for a caller that did not lock.
	"""
	db.add_all(
		RecordStatusChange(
			record_id=record_id,
			collection_id=collection_id,
			from_status=from_status,
			to_status=to_status,
			changed_by=actor,
		)
		for record_id in record_ids
	)
