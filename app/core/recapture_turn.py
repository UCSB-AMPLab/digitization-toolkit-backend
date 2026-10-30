"""The recapture turn: while records are pending anywhere on the unit, new volumes alternate with finished recaptures.

Finishing every recapture of one volume opens the turn for one new volume (a
volume with no records); the first capture into a new volume returns the turn
to recaptures. Turns do not accumulate, and the turn belongs to the unit, not
to a user. Three system-log events carry the state, so no migration is needed:

  recapture_volume_completed  a retake left its volume with no pending record
  rescan_volume_completed     a full rescan was sent to review for the first time
  new_volume_started          a capture created the first record of a volume

A full rescan is a volume whose queue action (fisqua.action in
custom_attributes, written by the list import and the queue sync) is
"full-rescan": it redoes a volume digitised before, so it is rework, not new
material. Its first capture is never refused for the turn and writes no
new_volume_started, so it leaves the turn as it was; until its first
promotion, finishing its retakes writes no recapture_volume_completed either,
so a rescan earns one turn, not two. After it, its retakes earn turns as any
volume's do. Sending it to review
(POST /collections/{id}/promote, app/api/collections.py) completes it as
finishing a volume's recaptures does, and opens the turn for one new volume.
It is sent to review when none of its records is left captured, through
whichever route moved the last one: POST /collections/{id}/promote, PATCH
/records/{id}/status or POST /records/bulk-status, each of which calls
award_rescan_turns. The first time that holds, rescan_volume_completed is
written, and never again, so taking the volume back and sending it again
opens no second turn.

A record is pending while its status is rejected, or while it is captured
and a current image still carries a rejection_id: the state the
prepare-for-retake transition (PATCH /records/{id}/status, rejected ->
captured) leaves it in until a capture replaces the image. pending_clause and
record_is_pending are that one definition, used by the summary, the turn,
the completion test and the promotion refusal.

The turn is "free" when no record is pending; otherwise "new" when the
newest completion, of either kind, is newer than the newest start, else
"recaptures". Newer
means a higher system_logs id, not a later created_at: both events are
inserted while lock_turn is held, so their ids are in the order the turn
changed, while created_at defaults to the start of each transaction.

The capture endpoints (app/api/cameras.py) refuse an operator's capture that
would start a new volume during the recaptures turn; an admin's is let
through, and a full rescan's is never refused. They take lock_turn before reading the turn or writing either event,
after the record and volume row locks they already hold, so two operators
cannot both start a new volume on one completion. Promotion of a volume with
any pending record is refused separately (app/api/collections.py). The
routes that send a full rescan to review hold their record locks, then take
the volume's row, then lock_turn (award_rescan_turns): the order of every
path that takes lock_turn.

Version: 0.3.1
"""
from datetime import datetime, timezone
import json
import re
from typing import Optional

from sqlalchemy import and_, exists, func, or_, text
from sqlalchemy.orm import Session

from app.models.collection import Collection
from app.models.record import Record, RecordImage
from app.models.system_log import SystemLog


COMPLETED_ACTION = "recapture_volume_completed"
RESCAN_COMPLETED_ACTION = "rescan_volume_completed"
STARTED_ACTION = "new_volume_started"
_COMPLETION_ACTIONS = (COMPLETED_ACTION, RESCAN_COMPLETED_ACTION)

FULL_RESCAN = "full-rescan"

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


def pending_clause(record=Record):
	"""SQL for "this record is pending"; `record` may be an alias of Record."""
	marked = exists().where(
		RecordImage.record_id == record.id,
		RecordImage.is_current.is_(True),
		RecordImage.rejection_id.isnot(None),
	)
	return or_(record.status == "rejected", and_(record.status == "captured", marked))


def record_is_pending(rec: Record) -> bool:
	"""The same test as pending_clause, on a loaded record."""
	if rec.status == "rejected":
		return True
	return rec.status == "captured" and any(
		img.is_current and img.rejection_id is not None for img in rec.images)


def pending_records(db: Session) -> int:
	"""How many records are pending on the unit."""
	return db.query(func.count(Record.id)).filter(pending_clause()).scalar() or 0


def volume_pending(db: Session, collection_id: int) -> int:
	"""How many records of the volume are pending."""
	return (
		db.query(func.count(Record.id))
		.filter(Record.collection_id == collection_id, pending_clause())
		.scalar() or 0
	)


def volume_is_empty(db: Session, collection_id: int) -> bool:
	"""Whether the volume has no record. Read under the volume's row lock, which every path adding a record takes."""
	return db.query(Record.id).filter(Record.collection_id == collection_id).first() is None


def volume_is_full_rescan(volume: Collection) -> bool:
	"""Whether the volume's queue action is a full rescan.

	True only when custom_attributes parse as a JSON object whose `fisqua` is
	an object with action exactly "full-rescan". Absent, unparsable or any
	other value is not a rescan, so such a volume takes the turn as any new
	volume does.
	"""
	try:
		attrs = json.loads(volume.custom_attributes or "")
	except ValueError:
		return False
	if not isinstance(attrs, dict):
		return False
	fisqua = attrs.get("fisqua")
	return isinstance(fisqua, dict) and fisqua.get("action") == FULL_RESCAN


def has_event(db: Session, action: str, collection_id: int) -> bool:
	"""Whether a turn event of this kind was ever recorded for the volume.

	Matched on the detail record_event writes, "collection_id=<id> ...", with
	the space, so volume 1 does not match volume 10's events.
	"""
	return db.query(SystemLog.id).filter(
		SystemLog.action == action,
		SystemLog.detail.like(f"collection\\_id={collection_id} %", escape="\\"),
	).first() is not None


def lock_rescan_volumes(db: Session, collection_ids) -> list:
	"""Lock the rows of the full rescans among the volumes, in id order; return their ids.

	Called by the routes that move records out of captured, after their
	record locks and before any status change, with the volumes the changes
	will touch; award_rescan_turns then takes the ids it returns. Volumes
	that are not full rescans are dropped before any lock, so they take
	none. Taking a rescan's row here, before the status changes write their
	history rows (which take a key share lock on the volume), means two
	requests over one rescan queue on its row instead of each holding a key
	share lock and waiting for the other's to upgrade.
	"""
	from app.core import volume_stem

	with db.no_autoflush:
		rescans = sorted({
			cid for cid in collection_ids
			if cid is not None and (volume := db.get(Collection, cid)) is not None
			and volume_is_full_rescan(volume)
		})
	for cid in rescans:
		volume_stem.locked_volume_query(db, cid).first()
	return rescans


def award_rescan_turns(db: Session, rescans: list, actor: Optional[str]) -> None:
	"""Write rescan_volume_completed for each locked rescan that is now sent to review.

	`rescans` is what lock_rescan_volumes returned before the caller's status
	changes. A volume is due when it carries no such event yet, has at least
	one record and none left captured. The caller's status changes are
	flushed first, so every key share lock their history rows take on any
	volume is held before the turn lock; then the turn lock, once. The order
	on every path is records, then the rescans' rows in id order, then the
	turn lock, and a path holding the turn lock never waits for a volume row,
	so this cannot deadlock against a retake, which holds its volume's row
	and then asks for the turn.
	"""
	if not rescans:
		return
	db.flush()
	lock_turn(db)
	for cid in rescans:
		if has_event(db, RESCAN_COMPLETED_ACTION, cid):
			continue
		statuses = [s for (s,) in db.query(Record.status).filter(Record.collection_id == cid).distinct()]
		if statuses and "captured" not in statuses:
			record_event(db, RESCAN_COMPLETED_ACTION, db.get(Collection, cid), actor)


def _newest(db: Session, *actions: str) -> Optional[SystemLog]:
	return (
		db.query(SystemLog)
		.filter(SystemLog.action.in_(actions))
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
	completed = _newest(db, *_COMPLETION_ACTIONS)
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
	"""Add one of the three turn events to the caller's transaction, which commits it with the capture.

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
