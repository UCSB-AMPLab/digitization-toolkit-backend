"""The digitisation queue synced with Fisqua (contract 1.7): the unit's users, the queue, and its acknowledgement.

Fisqua holds the queue's contents, order, jobs, notes and assignments; the
unit holds its users and what it has captured. Every call goes through the
transport the status poll uses (app.core.fisqua_http), to the same host
with the same token, under the same call budget, configuration stop and
offsite hold (app.core.fisqua_outbox).

* **Users** (send_operators). PUT /api/capture/v1/operators with the
  unit's admins and operators, each by username, which is also the
  display name; no email is sent, and reviewers, who cannot capture, are
  left out. Sent when the backend starts (FisquaStatusScheduler), after
  each user created, changed or deleted through the users routes
  (notify_operators_changed, run after the response), and at least once a
  day. A failed send is logged and leaves the list due, so the next poll
  tick sends it; it never fails the route. A list Fisqua refuses (422,
  logged with its problems, or 413), or one longer than MAX_OPERATORS, is
  not sent again until the list itself is different.
* **The queue** (sync_fisqua_queue), at each status poll tick. GET
  /api/capture/v1/queue with If-None-Match holding the last revision
  acknowledged; a 304 does nothing. A 200 is applied and acknowledged.
* **Applying** (apply_queue). Each item that is not withdrawn is checked as
  a list import checks an item (app.core.list_import.check_item). When a
  volume on the unit has its referenceCode as external_id, that volume's
  sequence, assignee, and the `fisqua` fields of its custom_attributes
  (action, notes and the item's other fields that have no column) are
  updated, and a hidden volume is shown again; its project, stem, reference
  code, title and dates are never changed. Otherwise the volume is created
  as an import creates one (list_import.create_volume), in the project named
  `listName`, which is created when there is none. An item the import would
  reject, whose assignee is not a username, or whose `retakes` is not the
  agreed shape (a spot recapture's [{captuaRecordId, pairId, side}]) is
  skipped with that reason. The retakes are kept with the item for Cola;
  they never change a record's status. The assignee is stored as sent: a
  retake entry arrives unassigned (null), and none is made up for it.
  A withdrawn item whose volume has no records hides the volume from Cola
  (collections.queue_hidden_at; the row is kept); one whose volume has
  records changes nothing; both are acknowledged as applied. A withdrawn
  item with no volume on the unit is skipped (withdrawn_not_on_unit). No
  record's status is ever changed here: rejections arrive only as QC
  decisions (app.core.fisqua_decisions).
* **Cola**. Each applied revision is stored as the list QUEUE_LIST_ID
  (list_import.store_queue_list), so GET /imports/digitisation-lists
  returns it beside file imports, with each item's assignee and withdrawn
  flag, every field of each item kept in its list.json. It is dated by
  when it was applied, so a volume's notes come from the newest of its file
  imports and queue revisions, as the band already picks them.
* **Acknowledging**. POST /api/capture/v1/queue/ack with the revision and
  the applied and skipped items. The body is kept in `queue-state.json`
  before it is posted, and is not posted when it cannot be kept (the
  revision is then applied again at the next tick); while it is not acknowledged each tick posts the
  same body again and asks for nothing else. The revision is kept only once
  Fisqua has acknowledged it. itemIds the answer lists as `unknown` are
  logged once and left out of every later ack. An ack Fisqua refuses (400,
  413, or 422 with any rule, `revision` included) is logged with its
  problems and dropped with the kept revision, so the next tick asks for
  the queue afresh. A revision that cannot be stored for Cola is
  not acknowledged, and is applied again at the next tick.
* **A Fisqua without 1.7.** A 404 or 405 on any of these paths is
  `unsupported`: logged once per process, asked again at the next tick, and
  never a configuration stop or a Retry-After wait, so handovers, status and
  decisions go on. Any other error takes the link's usual handling.

Version: 0.3.0
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core import fisqua_contract as fc
from app.core import fisqua_outbox, list_import, offsite_hold
from app.core.fisqua_outbox import NullTransport, TransportResult

logger = logging.getLogger(__name__)

# The list id the queue is stored under, the same on every unit.
QUEUE_LIST_ID = list_import.QUEUE_LIST_ID
QUEUE_CONTRACT_VERSION = "1.7"
STATE_FILE_NAME = "queue-state.json"
# created_by of a volume or project the sync creates: no unit user did.
QUEUE_ACTOR = "fisqua-queue"
OPERATORS_INTERVAL_SECONDS = 24 * 3600
ASSIGNEE_MAX_LENGTH = 150

# Skip reasons of the queue's own, beside list_import.REASONS.
QUEUE_REASONS = {
    "invalid_assignee": f"The assignee is not null or a username of at most {ASSIGNEE_MAX_LENGTH} characters.",
    "withdrawn_not_on_unit": "The item was withdrawn and no volume on this unit has its referenceCode.",
    "list_name_refused": "The project named after listName cannot be created on this unit.",
    "invalid_retakes": (
        "The retakes are not a list of objects, each with an integer captuaRecordId, a pairId that is "
        "a non-empty string or null, and a side of left, right or both."
    ),
}

RETAKE_SIDES = ("left", "right", "both")

# The item fields the queue keeps in columns or uses only to decide, left out of custom_attributes.fisqua.
_NOT_KEPT_IN_ATTRIBUTES = (fc.F_ASSIGNEE, fc.F_WITHDRAWN)

_monotonic = time.monotonic

# Whether this process has logged that Fisqua has no contract 1.7 (a 404 or
# 405 on a 1.7 path). Logged once; every tick still asks again.
_unsupported_logged = False


def _note_unsupported(answer: TransportResult) -> None:
    global _unsupported_logged
    if _unsupported_logged:
        return
    _unsupported_logged = True
    logger.info("Fisqua answered HTTP %s on a contract 1.7 path: it has no digitisation queue for this unit yet. "
                "Asked again at each status poll; not logged again until the backend restarts.",
                answer.status_code)


def get_transport():
    """The transport the status poll uses (a seam for tests)."""
    return fisqua_outbox.get_transport()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@dataclass
class _OperatorsState:
    """Whether the unit's users are due to be sent, and when they last were (monotonic seconds)."""

    due: bool = True
    sent_at: Optional[float] = None
    # The body Fisqua refused (422 or 413), or one over MAX_OPERATORS: not
    # sent again, by a tick, a route or the daily send, until the list differs.
    refused_body: Optional[dict] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def is_due(self) -> bool:
        if self.due:
            return True
        return self.sent_at is not None and _monotonic() - self.sent_at >= OPERATORS_INTERVAL_SECONDS


# Due at process start, so the first send happens whatever else runs.
_operators = _OperatorsState()


def operators_body(db: Session) -> dict:
    """The PUT body: every admin and operator, active or not, by username."""
    from app.models.user import User

    users = (db.query(User.username, User.role, User.is_active)
             .filter(User.role.in_(fc.OPERATOR_ROLES))
             .order_by(User.username)
             .all())
    return {fc.F_OPERATORS: [
        {fc.F_USERNAME: username, fc.F_DISPLAY_NAME: username, fc.F_OPERATOR_ROLE: role,
         fc.F_ACTIVE: bool(active)}
        for username, role, active in users
    ]}


def mark_operators_due() -> None:
    _operators.due = True


def _gate() -> Optional[dict]:
    """The answer for a call that is not to be made now, or None."""
    if not fisqua_outbox.fisqua_configured():
        return {"status": "disabled"}
    if offsite_hold.held("fisqua queue sync"):
        return {"status": "held", "hold_file": str(offsite_hold.hold_file())}
    return None


def _call(transport, method: str, *args) -> TransportResult:
    try:
        answer = getattr(transport, method)(*args)
    except Exception as exc:
        answer = TransportResult(outcome="error", error=f"{type(exc).__name__}: {exc}")
    return fisqua_outbox._redacted(answer)


def send_operators(db: Session, *, transport=None, now: Optional[datetime] = None,
                   only_if_due: bool = False) -> dict:
    """PUT the unit's users to Fisqua. Never raises.

    A transient failure leaves them due for the next tick (a 429 or other
    Retry-After also holds the link back, as for every call). A body Fisqua
    refuses (422, logged with its problems, or 413), or one longer than
    MAX_OPERATORS, is not sent again until the list it reads is different.
    """
    gated = _gate()
    if gated is not None:
        return gated
    transport = transport if transport is not None else get_transport()
    if isinstance(transport, NullTransport):
        return {"status": "no_transport"}
    now = fisqua_outbox._naive(now) or fisqua_outbox._now()
    with _operators.lock:
        refused = _operators.refused_body
        if only_if_due and refused is None and not _operators.is_due():
            return {"status": "not_due"}
        try:
            if fisqua_outbox.config_error() is not None:
                return {"status": "config_error"}
            body = operators_body(db)
            db.rollback()
        except Exception:
            db.rollback()
            logger.exception("Reading the unit's users for Fisqua failed; sent at the next tick")
            _operators.due = True
            return {"status": "error"}
        if refused is not None:
            if body == refused:
                return {"status": "refused"}
            _operators.refused_body = None
        if len(body[fc.F_OPERATORS]) > fc.MAX_OPERATORS:
            logger.warning("The unit has %d admins and operators, more than the %d Fisqua takes; the list is "
                           "not sent until it changes", len(body[fc.F_OPERATORS]), fc.MAX_OPERATORS)
            _operators.refused_body = body
            _operators.due = False
            return {"status": "refused"}
        if not fisqua_outbox.call_budget.take():
            _operators.due = True
            return {"status": "rate_limited"}
        answer = _call(transport, "put_operators", body)
        if answer.outcome == "config_error":
            fisqua_outbox.record_config_error(answer.status_code, answer.reason, now)
            _operators.due = True
            return {"status": "config_error"}
        if answer.outcome == "unsupported":
            _note_unsupported(answer)
            _operators.due = True
            return {"status": "unsupported"}
        if answer.outcome == "refused":
            logger.warning("Fisqua refused the unit's users (HTTP %s): %s; not sent again until the list changes",
                           answer.status_code,
                           fisqua_outbox.scrub(json.dumps(answer.problems, ensure_ascii=False)
                                               if answer.problems else (answer.error or "no problems given")))
            _operators.refused_body = body
            _operators.due = False
            return {"status": "refused"}
        if answer.outcome != "delivered":
            logger.warning("Sending the unit's users to Fisqua failed (%s); sent again at the next tick",
                           fisqua_outbox.scrub(answer.error or answer.outcome))
            if answer.retry_after is not None:
                fisqua_outbox.call_budget.cool_down(answer.retry_after)
            _operators.due = True
            return {"status": "error"}
        _operators.due = False
        _operators.sent_at = _monotonic()
        return {"status": "sent", "operators": len(body[fc.F_OPERATORS])}


def notify_operators_changed() -> None:
    """After a user is created, changed or deleted: send the users now. Never raises.

    Run as a background task after the route's response, so a slow or
    failing Fisqua never fails or delays the route. A failure is logged and
    the users stay due for the next poll tick.
    """
    mark_operators_due()
    try:
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            send_operators(db)
        finally:
            db.close()
    except Exception:
        logger.exception("Sending the unit's users to Fisqua after a change raised; sent at the next tick")


# ---------------------------------------------------------------------------
# The state: the last acknowledged revision and an ack still owed
# ---------------------------------------------------------------------------

def _state_path():
    return list_import.reports_dir() / QUEUE_LIST_ID / STATE_FILE_NAME


def _read_state() -> dict:
    path = _state_path()
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError):
        logger.warning("The queue state %s cannot be read; the queue is asked for whole", path)
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    list_import._replace_file(path, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))


# ---------------------------------------------------------------------------
# Applying a revision
# ---------------------------------------------------------------------------

class QueueUnreadable(Exception):
    """The queue answer as a whole cannot be applied; nothing was written. Not acknowledged."""


class QueueRetry(Exception):
    """Applying stopped for a reason that may pass (the project lock); not acknowledged, tried at the next tick."""


def _valid_assignee(value: Any) -> bool:
    return value is None or (isinstance(value, str) and 0 < len(value) <= ASSIGNEE_MAX_LENGTH
                             and "\x00" not in value)


def _valid_retakes(item: dict) -> bool:
    """Whether the item's `retakes`, when it has the field, is the agreed shape.

    Absent is valid (every item but a spot recapture). Present, it is a
    list (possibly empty) of objects, each with captuaRecordId an integer
    (not a boolean), pairId a non-empty string without NUL or null (a
    single-image opening), and side one of
    RETAKE_SIDES; other keys in an entry are ignored. null is not a list.
    """
    if fc.F_RETAKES not in item:
        return True
    retakes = item[fc.F_RETAKES]
    if not isinstance(retakes, list):
        return False
    for entry in retakes:
        if not isinstance(entry, dict):
            return False
        record_id = entry.get(fc.F_CAPTUA_RECORD_ID)
        pair_id = entry.get(fc.F_PAIR_ID)
        if isinstance(record_id, bool) or not isinstance(record_id, int):
            return False
        # null for a single-image opening.
        if pair_id is not None and (not isinstance(pair_id, str) or not pair_id or "\x00" in pair_id):
            return False
        if entry.get(fc.F_SIDE) not in RETAKE_SIDES:
            return False
    return True


def _merge_attributes(current: Optional[str], fisqua: dict) -> Optional[str]:
    """custom_attributes with `fisqua` merged into its `fisqua` object; None when `current` is not a JSON object."""
    if current is None or not current.strip():
        attrs: dict = {}
    else:
        try:
            attrs = json.loads(current)
        except ValueError:
            return None
        if not isinstance(attrs, dict):
            return None
    kept = attrs.get("fisqua")
    kept = dict(kept) if isinstance(kept, dict) else {}
    # The retakes are the entry's current ones: an entry without them drops
    # the ones an earlier revision left.
    kept.pop(fc.F_RETAKES, None)
    kept.update(fisqua)
    attrs["fisqua"] = kept
    return json.dumps(attrs, ensure_ascii=False)


def _has_records(db: Session, collection_id: int) -> bool:
    from app.models.record import Record

    return db.query(Record.id).filter(Record.collection_id == collection_id).first() is not None


def _volume_by_code(db: Session, code: Any):
    from app.models.collection import Collection

    if not isinstance(code, str) or not code or "\x00" in code:
        return None
    return db.query(Collection).filter(Collection.external_id == code).first()


def apply_queue(db: Session, body: Any, revision: str, now: datetime) -> tuple[dict, dict, dict]:
    """Apply one queue answer; return (the ack body, the report, the list document for Cola).

    Raises QueueUnreadable when the answer cannot be read as a queue, and
    QueueRetry when the project lock cannot be taken; in both cases
    nothing more is applied and nothing is acknowledged.
    """
    if not isinstance(body, dict):
        raise QueueUnreadable("the answer is not an object")
    if list_import._holds_lone_surrogate(body):
        raise QueueUnreadable("a string in the answer holds a lone surrogate")
    list_name = body.get(fc.F_LIST_NAME)
    items = body.get(fc.F_ITEMS)
    document = {
        "contract": list_import.CONTRACT_NAME,
        "kind": list_import.LIST_KIND,
        "version": QUEUE_CONTRACT_VERSION,
        "id": QUEUE_LIST_ID,
        "source": list_import.QUEUE_SOURCE,
        "revision": revision,
        "list": {"name": list_name},
        "items": items,
    }
    try:
        list_import.check_envelope(document)
    except list_import.ListRefused as exc:
        raise QueueUnreadable(str(exc)) from exc

    def withdrawn(item: Any) -> bool:
        return isinstance(item, dict) and item.get(fc.F_WITHDRAWN) is True

    # Duplicates are looked for among the live items only: Fisqua may send a
    # withdrawn entry and a new one for the same description together.
    first_code, first_stem = list_import.first_positions(
        [None if withdrawn(item) else item for item in items])

    applied: list[dict] = []
    skipped: list[dict] = []
    counts = {"created": 0, "updated": 0, "hidden": 0}
    live_codes: set = set()
    project_id: Optional[int] = None
    project_refused = False

    def item_id_of(position: int, item: Any) -> Optional[str]:
        value = item.get(fc.F_ITEM_ID) if isinstance(item, dict) else None
        if isinstance(value, str) and value:
            return value
        logger.warning("Queue revision %s: item %d has no itemId and cannot be acknowledged; left out",
                       revision, position)
        return None

    def skip(item_id: str, reason: str) -> None:
        skipped.append({fc.F_ITEM_ID: item_id, fc.F_REASON: reason})

    for position, item in enumerate(items, start=1):
        if withdrawn(item):
            continue
        item_id = item_id_of(position, item)
        if item_id is None:
            continue
        cleaned = {k: v for k, v in item.items() if k not in _NOT_KEPT_IN_ATTRIBUTES}
        reason, values = list_import.check_item(position, cleaned, first_code, first_stem)
        if reason is not None:
            skip(item_id, reason)
            continue
        assignee = item.get(fc.F_ASSIGNEE)
        if not _valid_assignee(assignee):
            skip(item_id, "invalid_assignee")
            continue
        if not _valid_retakes(item):
            skip(item_id, "invalid_retakes")
            continue
        code = values["external_id"]
        live_codes.add(code)
        fisqua = json.loads(values["custom_attributes"])["fisqua"]
        try:
            volume = _volume_by_code(db, code)
            if volume is not None:
                attributes = _merge_attributes(volume.custom_attributes, fisqua)
                if attributes is None:
                    logger.warning("Queue revision %s: volume %s keeps its custom_attributes, which are not a "
                                   "JSON object, so its queue notes and action are not stored there",
                                   revision, volume.id)
                else:
                    volume.custom_attributes = attributes
                volume.sequence = values["sequence"]
                volume.assignee_username = assignee
                volume.queue_hidden_at = None
                volume_id = volume.id
                db.commit()
                counts["updated"] += 1
                applied.append({fc.F_ITEM_ID: item_id, fc.F_COLLECTION_ID: volume_id})
                continue
        except SQLAlchemyError:
            logger.exception("Queue revision %s: updating the volume of item %d failed", revision, position)
            db.rollback()
            skip(item_id, "refused_by_database")
            continue

        if project_id is None and not project_refused:
            try:
                project, _created = list_import.project_for(db, list_name, QUEUE_ACTOR)
                project_id = project.id
            except list_import.ProjectLockUnavailable as exc:
                raise QueueRetry(str(exc)) from exc
            except list_import.ListRefused as exc:
                logger.warning("Queue revision %s: the project %r cannot be created: %s", revision, list_name, exc)
                project_refused = True
        if project_refused:
            skip(item_id, "list_name_refused")
            continue
        volume_id, reason = list_import.create_volume(
            db, QUEUE_LIST_ID, position, project_id, QUEUE_ACTOR, {**values, "assignee_username": assignee})
        if reason is not None:
            skip(item_id, reason)
            continue
        counts["created"] += 1
        applied.append({fc.F_ITEM_ID: item_id, fc.F_COLLECTION_ID: volume_id})

    for position, item in enumerate(items, start=1):
        if not withdrawn(item):
            continue
        item_id = item_id_of(position, item)
        if item_id is None:
            continue
        try:
            volume = _volume_by_code(db, item.get("referenceCode"))
            if volume is None:
                db.rollback()
                skip(item_id, "withdrawn_not_on_unit")
                continue
            volume_id = volume.id
            # A live entry for the same code in this revision decides, and a
            # volume with records keeps everything: nothing is changed.
            if volume.external_id not in live_codes and not _has_records(db, volume_id) \
                    and volume.queue_hidden_at is None:
                volume.queue_hidden_at = now
                db.commit()
                counts["hidden"] += 1
            else:
                db.rollback()
        except SQLAlchemyError:
            logger.exception("Queue revision %s: hiding the volume of item %d failed", revision, position)
            db.rollback()
            skip(item_id, "refused_by_database")
            continue
        applied.append({fc.F_ITEM_ID: item_id, fc.F_COLLECTION_ID: volume_id})

    project = list_import.find_project(db, list_name)
    report = {
        "listId": QUEUE_LIST_ID,
        "listName": list_name,
        "projectId": project.id if project is not None else None,
        "projectName": project.name if project is not None else None,
        "revision": revision,
        "importedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "importedBy": None,
        **counts,
        "skipped": len(skipped),
        "applied": applied,
        "skippedItems": skipped,
    }
    db.rollback()
    ack = {fc.F_REVISION: revision, fc.F_APPLIED: applied, fc.F_SKIPPED: skipped}
    return ack, report, document


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------

_queue_lock = threading.Lock()


# itemIds Fisqua answered an ack as not knowing: logged once, and left out
# of every later ack this process sends.
_unknown_item_ids: set = set()


def _without_unknown(ack: dict) -> dict:
    """The ack without the items Fisqua said it does not know."""
    if not _unknown_item_ids:
        return ack
    return {**ack,
            fc.F_APPLIED: [a for a in ack[fc.F_APPLIED] if a.get(fc.F_ITEM_ID) not in _unknown_item_ids],
            fc.F_SKIPPED: [k for k in ack[fc.F_SKIPPED] if k.get(fc.F_ITEM_ID) not in _unknown_item_ids]}


def _note_unknown(response: Any) -> None:
    unknown = response.get(fc.F_UNKNOWN) if isinstance(response, dict) else None
    if not isinstance(unknown, list):
        return
    new = [i for i in unknown if isinstance(i, str) and i not in _unknown_item_ids]
    if new:
        _unknown_item_ids.update(new)
        logger.warning("Fisqua does not know %d acknowledged queue item(s): %s; not acknowledged again",
                       len(new), fisqua_outbox.scrub(", ".join(new)))


def _revision_refused(answer: TransportResult) -> bool:
    return answer.outcome == "refused" and any(
        isinstance(p, dict) and p.get(fc.F_RULE) == fc.RULE_REVISION for p in answer.problems or [])


def _post_ack(transport, body: dict, now: datetime) -> str:
    """`acknowledged`, `dropped` (refused: 400, 413 or 422), `config_error` or `error`."""
    if not fisqua_outbox.call_budget.take():
        return "error"
    answer = _call(transport, "post_queue_ack", body)
    if answer.outcome == "config_error":
        fisqua_outbox.record_config_error(answer.status_code, answer.reason, now)
        return "config_error"
    if answer.outcome == "refused":
        # A body Fisqua refuses (400, 413, or 422 with any rule, `revision`
        # included) is not made acceptable by being sent again.
        logger.warning("Fisqua refused the ack of queue revision %s (HTTP %s%s): %s; the ack is dropped and "
                       "the queue asked for afresh", fisqua_outbox.scrub(body.get(fc.F_REVISION)),
                       answer.status_code,
                       ", rule revision: malformed or later than Fisqua's" if _revision_refused(answer) else "",
                       fisqua_outbox.scrub(json.dumps(answer.problems, ensure_ascii=False)
                                           if answer.problems else (answer.error or "no problems given")))
        return "dropped"
    if answer.outcome == "unsupported":
        _note_unsupported(answer)
        return "error"
    if answer.outcome != "acknowledged":
        logger.warning("Acknowledging queue revision %s to Fisqua failed (%s); posted again at the next tick",
                       fisqua_outbox.scrub(body.get(fc.F_REVISION)),
                       fisqua_outbox.scrub(answer.error or answer.outcome))
        if answer.retry_after is not None:
            fisqua_outbox.call_budget.cool_down(answer.retry_after)
        return "error"
    _note_unknown(answer.response)
    return "acknowledged"


def _settle_ack(transport, body: dict, now: datetime) -> Optional[dict]:
    """Post an owed ack; None once acknowledged (the revision then kept), else the tick's answer."""
    outcome = _post_ack(transport, body, now)
    if outcome == "config_error":
        return {"status": "config_error"}
    if outcome == "dropped":
        try:
            _write_state({"revision": None, "pendingAck": None})
        except OSError:
            logger.exception("Could not drop the refused queue ack; it is posted again at the next tick")
        return {"status": "ack_dropped", "revision": body.get(fc.F_REVISION)}
    if outcome != "acknowledged":
        return {"status": "ack_failed", "revision": body.get(fc.F_REVISION)}
    try:
        _write_state({"revision": body.get(fc.F_REVISION), "pendingAck": None})
    except OSError:
        logger.exception("Could not keep the acknowledged queue revision; the queue is asked for whole next")
    return None


def sync_fisqua_queue(db: Session, *, transport=None, now: Optional[datetime] = None) -> dict:
    """One tick: send the users when due, then poll, apply and acknowledge the queue. Never raises."""
    gated = _gate()
    if gated is not None:
        return gated
    transport = transport if transport is not None else get_transport()
    if isinstance(transport, NullTransport):
        return {"status": "no_transport"}
    now = fisqua_outbox._naive(now) or fisqua_outbox._now()
    with _queue_lock:
        try:
            if fisqua_outbox.config_error() is not None:
                return {"status": "config_error"}
            operators = send_operators(db, transport=transport, now=now, only_if_due=True)
            if operators["status"] == "config_error":
                return {"status": "config_error", "operators": operators["status"]}
            if fisqua_outbox.call_budget.cooling():
                return {"status": "cooling_down", "operators": operators["status"]}

            state = _read_state()
            revision = state.get("revision") if isinstance(state.get("revision"), str) else None
            pending = state.get("pendingAck")
            if isinstance(pending, dict):
                stopped = _settle_ack(transport, pending, now)
                if stopped is not None:
                    return {**stopped, "operators": operators["status"]}
                revision = pending.get(fc.F_REVISION)

            if not fisqua_outbox.call_budget.take(reserve=fc.DECISIONS_RESERVED_CALLS):
                return {"status": "rate_limited", "operators": operators["status"]}
            answer = _call(transport, "get_queue", revision)
            if answer.outcome == "config_error":
                fisqua_outbox.record_config_error(answer.status_code, answer.reason, now)
                return {"status": "config_error", "operators": operators["status"]}
            if answer.outcome == "not_modified":
                return {"status": "unchanged", "revision": revision, "operators": operators["status"]}
            if answer.outcome == "unsupported":
                _note_unsupported(answer)
                return {"status": "unsupported", "operators": operators["status"]}
            if answer.outcome != "queue":
                logger.warning("Asking Fisqua for the digitisation queue failed: %s",
                               fisqua_outbox.scrub(answer.error or answer.outcome))
                if answer.retry_after is not None:
                    fisqua_outbox.call_budget.cool_down(answer.retry_after)
                return {"status": "error", "operators": operators["status"]}

            body = answer.response
            new_revision = body.get(fc.F_REVISION) if isinstance(body, dict) else None
            if not isinstance(new_revision, str) or not new_revision:
                new_revision = answer.etag
            if not new_revision:
                logger.warning("Fisqua's digitisation queue came without a revision; not applied")
                return {"status": "unreadable", "operators": operators["status"]}
            try:
                ack, report, document = apply_queue(db, body, new_revision, now)
            except QueueUnreadable as exc:
                db.rollback()
                logger.warning("Fisqua's digitisation queue revision %s cannot be applied: %s; not acknowledged",
                               fisqua_outbox.scrub(new_revision), fisqua_outbox.scrub(exc))
                return {"status": "unreadable", "operators": operators["status"]}
            except QueueRetry as exc:
                db.rollback()
                logger.warning("Applying Fisqua's digitisation queue stopped: %s; tried again at the next tick", exc)
                return {"status": "error", "operators": operators["status"]}

            try:
                list_import.store_queue_list(QUEUE_LIST_ID, document, report)
            except OSError:
                logger.exception("Could not store queue revision %s for Cola; not acknowledged, applied again "
                                 "at the next tick", fisqua_outbox.scrub(new_revision))
                return {"status": "store_failed", "operators": operators["status"]}
            from app.core.audit import log_event

            log_event(db, level="INFO", category="activity", action="fisqua_queue_applied",
                      actor=QUEUE_ACTOR, subject=report["listName"],
                      detail=(f"revision {new_revision}: {report['created']} created, {report['updated']} "
                              f"updated, {report['hidden']} hidden, {report['skipped']} skipped")[:2000])
            ack = _without_unknown(ack)
            try:
                _write_state({"revision": revision, "pendingAck": ack})
            except OSError:
                # Posted without being kept, a lost answer could not be
                # retried with the same body. Applying is idempotent: the
                # next tick asks under the old revision, applies again and
                # tries to keep the ack again.
                logger.exception("Could not keep the ack of queue revision %s, so it is not posted; applied "
                                 "again at the next tick", fisqua_outbox.scrub(new_revision))
                return {"status": "ack_not_kept", "operators": operators["status"]}
            stopped = _settle_ack(transport, ack, now)
            if stopped is not None:
                return {**stopped, "operators": operators["status"]}
            return {"status": "applied", "revision": new_revision, "created": report["created"],
                    "updated": report["updated"], "hidden": report["hidden"], "skipped": report["skipped"],
                    "operators": operators["status"]}
        except Exception:
            db.rollback()
            logger.exception("The Fisqua queue sync raised")
            return {"status": "error"}
