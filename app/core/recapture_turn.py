"""The recapture turn: while records are rejected anywhere on the unit, new volumes alternate with finished recaptures.

Finishing every recapture of one volume opens the turn for one new volume (a
volume with no records); the first capture into a new volume returns the turn
to recaptures. Turns do not accumulate, and the turn belongs to the unit, not
to a user. Two system-log events carry the state, so no migration is needed:

  recapture_volume_completed  a retake left its volume with no rejected record
  new_volume_started          a capture created the first record of a volume

The turn is "free" when no record is rejected; otherwise "new" when the
newest completion is newer than the newest start, else "recaptures". Newer
means a higher system_logs id, not a later created_at: both events are
inserted while lock_turn is held, so their ids are in the order the turn
changed, while created_at defaults to the start of each transaction.

The capture endpoints (app/api/cameras.py) refuse an operator's capture that
would start a new volume during the recaptures turn; an admin's is let
through. They take lock_turn before reading the turn or writing either event,
after the record and volume row locks they already hold, so two operators
cannot both start a new volume on one completion. Promotion of a volume with
any rejected record is refused separately (app/api/collections.py).

Version: 0.2.0
"""
from datetime import datetime, timezone
import re
from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models.collection import Collection
from app.models.record import Record
from app.models.system_log import SystemLog


COMPLETED_ACTION = "recapture_volume_completed"
STARTED_ACTION = "new_volume_started"

# Any constant serves, as long as nothing else on the database takes the same
# advisory lock; no other code in Captua takes one.
_TURN_LOCK_KEY = 0x43415054  # "CAPT"

RECAPTURE_TURN_DETAIL = {
	"code": "recapture_turn",
	"message": (
		"The volume could not be started: it is the retake turn. "
		"Finish the retakes of one volume and try again."
	),
}

_DETAIL_COLLECTION = re.compile(r"collection_id=(\d+)")


def lock_turn(db: Session) -> None:
	"""Hold the unit's turn lock until the transaction ends.

	A PostgreSQL transaction-level advisory lock, released by the commit or
	rollback. SQLite, used only by the test suite, takes no such lock and
	serialises writers itself, so this is a no-op there.
	"""
	if db.get_bind().dialect.name == "postgresql":
		db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _TURN_LOCK_KEY})


def pending_records(db: Session) -> int:
	"""How many records are rejected on the unit, the count GET /records/to-retake/count gives."""
	return db.query(func.count(Record.id)).filter(Record.status == "rejected").scalar() or 0


def volume_pending(db: Session, collection_id: int) -> int:
	"""How many records of the volume are rejected."""
	return (
		db.query(func.count(Record.id))
		.filter(Record.collection_id == collection_id, Record.status == "rejected")
		.scalar() or 0
	)


def volume_is_empty(db: Session, collection_id: int) -> bool:
	"""Whether the volume has no record. Read under the volume's row lock, which every path adding a record takes."""
	return db.query(Record.id).filter(Record.collection_id == collection_id).first() is None


def _newest(db: Session, action: str) -> Optional[SystemLog]:
	return (
		db.query(SystemLog)
		.filter(SystemLog.action == action)
		.order_by(SystemLog.id.desc())
		.first()
	)


def turn(db: Session, pending: Optional[int] = None) -> dict:
	"""The turn as GET /records/to-retake/summary gives it: {"state", "unlockedBy"}.

	`pending` is the unit's rejected count, when the caller has it already.
	unlockedBy names the volume whose completion opened a "new" turn, by its
	current name; a volume deleted since keeps the name the event recorded.
	"""
	if pending is None:
		pending = pending_records(db)
	if pending == 0:
		return {"state": "free", "unlockedBy": None}
	completed = _newest(db, COMPLETED_ACTION)
	started = _newest(db, STARTED_ACTION)
	if completed is None or (started is not None and started.id > completed.id):
		return {"state": "recaptures", "unlockedBy": None}
	match = _DETAIL_COLLECTION.search(completed.detail or "")
	collection_id = int(match.group(1)) if match else None
	volume = db.get(Collection, collection_id) if collection_id is not None else None
	return {
		"state": "new",
		"unlockedBy": {
			"collectionId": collection_id,
			"name": volume.name if volume is not None else completed.subject,
		},
	}


def record_event(db: Session, action: str, volume: Collection, actor: Optional[str]) -> None:
	"""Add one of the two turn events to the caller's transaction, which commits it with the capture.

	Not app.core.audit.log_event, which commits at once: the event must stand
	or fall with the capture that caused it, and be inserted under lock_turn.
	"""
	db.add(SystemLog(
		level="INFO",
		category="capture",
		action=action,
		actor=actor[:150] if actor else None,
		subject=volume.name[:300] if volume.name else None,
		detail=f"collection_id={volume.id} filename_stem={volume.filename_stem}",
		created_at=datetime.now(timezone.utc),
	))
