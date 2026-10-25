"""Applying Fisqua's QC decisions to a volume's records (contract 1.2 §9.5).

Fisqua decides each opening (each Captua record): approved, or rejected for
retake with a reason, and may correct an image's rotation. Fisqua is
authoritative for this second layer, so its decision is applied over a local
one through the undo transitions. The status poll
(app.core.fisqua_outbox.poll_fisqua_status) reads a volume's set when its
`decisionsId` is new, and hands it here.

A set is applied only when its `manifestId` is the volume's current
handover, its newest delivered manifest. A set for an earlier one is stale:
it is noted (fisqua_status.decisions_stale_id, so it is not fetched again)
and not applied. A set whose `decisionsId` was applied already changes
nothing.

Per record, in one transaction, with the records' rows locked in id order
(as every path that locks several records does) and nothing else locked:

* approved: in_review -> approved; from rejected, rejected -> in_review ->
  approved; approved stays approved.
* rejected: in_review -> rejected with a RecordRejection (the reason, the
  note as its comment, rejected_by `Fisqua: <decidedBy>`), written by
  app.api.records.reject_locked_record, the path POST /records/{id}/reject
  takes; from approved, approved -> in_review -> rejected. A record already
  rejected with the same reason, note and reviewer is left as it is; one
  rejected otherwise goes rejected -> in_review -> rejected, so the
  rejection it carries is Fisqua's. It stays rejected until it is retaken.
* Every transition goes through app.api.records._apply_status_change as a
  reviewer named `Fisqua: <decidedBy>`, so the history row names the
  reviewer and that the decision came from Fisqua.
* Rotations: each current image of a decided record takes the correction
  the set gives it by captureId and role, in quarter-turns clockwise (1-3),
  or none when the set lists none for it. No file is touched.

A set also waits, unapplied and not stale, while a newer handover of the
volume is still on its way (an outbox row after the current one in
waiting_for_upload, ready, sending or failed): the records may already be
what that handover sends.

Skipped and reported, with nothing changed for that record: a record not in
the volume (or an id that is not one), a record that is `captured` (its
volume taken back, or it was retaken), a record whose current images are not
exactly those the decided manifest listed for it, compared image by image as
capture id, role and object key (`images_changed`: retaken since, or an
image of a pair removed, replaced or given the other role), a decision that is neither approved
nor rejected, and a rejection that POST /records/{id}/reject would refuse
(checked with the same RecordRejectRequest): a reason not in
app.schemas.record.PREDEFINED_REJECTION_REASONS, `other` with no note, or a
note longer than REJECTION_COMMENT_MAX_LENGTH (`note_too_long`).
An unusable rotation is skipped on its own.

What applying did is kept on fisqua_status (decisions_report) for the
volume page, and written to the system log as `fisqua_decisions_applied`.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app.core import fisqua_contract as fc

logger = logging.getLogger(__name__)

APPLIED_ACTION = "fisqua_decisions_applied"
STALE_ACTION = "fisqua_decisions_stale"
ACTOR_PREFIX = "Fisqua"
_ID_LIMIT = 64


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _record_id(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _shown_id(value: Any):
    """The record id as the report shows it: the number, or the value as text, cut short."""
    parsed = _record_id(value)
    return parsed if parsed is not None else str(value)[:_ID_LIMIT]


PENDING = ("waiting_for_upload", "ready", "sending", "failed")


def current_manifest_id(db: Session, collection_id: int) -> Optional[str]:
    """The manifest id of the volume's newest delivered outbox row: its current handover."""
    row = _current_row(db, collection_id)
    return row[1] if row is not None else None


def _current_row(db: Session, collection_id: int):
    """(id, manifest id) of the volume's newest delivered outbox row, or None."""
    from app.models.fisqua import FisquaOutbox

    return (db.query(FisquaOutbox.id, FisquaOutbox.manifest_id)
            .filter(FisquaOutbox.collection_id == collection_id, FisquaOutbox.state == "delivered")
            .order_by(FisquaOutbox.id.desc())
            .first())


def _newer_pending(db: Session, collection_id: int, row_id: int) -> bool:
    """Whether a handover after the current one is still on its way: a newer row not yet delivered or ended."""
    from app.models.fisqua import FisquaOutbox

    return db.query(FisquaOutbox.id).filter(
        FisquaOutbox.collection_id == collection_id, FisquaOutbox.id > row_id,
        FisquaOutbox.state.in_(PENDING)).first() is not None


def _listed_captures(db: Session, row_id: int) -> dict:
    """{record id: the images the manifest listed for it}, from the manifest stored on the row.

    Each image as (captureId, role, objectKey). Both images of a dual
    capture share one captureId, so the capture id alone cannot tell a pair
    from what is left of it; the role and the image's own object key can.
    """
    from app.models.fisqua import FisquaOutbox

    manifest = db.query(FisquaOutbox.manifest).filter(FisquaOutbox.id == row_id).scalar() or {}
    listed = {}
    for volume in manifest.get("volumes") or []:
        for record in volume.get("records") or []:
            rid = _record_id(record.get("captuaRecordId"))
            if rid is not None:
                listed[rid] = {(i.get("captureId"), i.get("role"), i.get("objectKey"))
                               for i in record.get("images") or [] if isinstance(i, dict)}
    return listed


def _current_images(db: Session, records) -> dict:
    """{record id: its current images as (capture id, role, capture key without the tenant)}.

    The key is the image's offsite capture object's, relative to the tenant
    prefix as the manifest's objectKey is; an image with no offsite object
    has None, which no manifest entry matches.
    """
    from app.models.offsite_object import OffsiteObject

    images = [img for rec in records for img in rec.images if img.is_current]
    keys = {}
    ids = [img.id for img in images]
    for start in range(0, len(ids), 500):
        for image_id, key in (db.query(OffsiteObject.record_image_id, OffsiteObject.object_key)
                              .filter(OffsiteObject.record_image_id.in_(ids[start:start + 500]),
                                      OffsiteObject.kind == "capture")):
            keys[image_id] = key.split("/", 1)[1] if "/" in key else key
    out = {rec.id: set() for rec in records}
    for img in images:
        out[img.record_id].add((img.capture_id, img.role, keys.get(img.id)))
    return out


def _why_not(decision: dict) -> Optional[str]:
    """Why a decision cannot be applied, from its content alone; None when it can.

    A rejection is checked by RecordRejectRequest, the request
    POST /records/{id}/reject validates a reviewer's rejection with: the
    reason one of PREDEFINED_REJECTION_REASONS, and `other` with a comment.
    """
    from pydantic import ValidationError

    from app.schemas.record import (
        PREDEFINED_REJECTION_REASONS, REJECTION_COMMENT_MAX_LENGTH, RecordRejectRequest,
    )

    kind = decision.get(fc.F_DECISION)
    if kind not in (fc.DECISION_APPROVED, fc.DECISION_REJECTED):
        return "invalid_decision"
    if kind == fc.DECISION_REJECTED:
        reason = decision.get(fc.F_REASON)
        if reason not in PREDEFINED_REJECTION_REASONS:
            return "invalid_reason"
        note = decision.get(fc.F_NOTE)
        if isinstance(note, str) and len(note) > REJECTION_COMMENT_MAX_LENGTH:
            return "note_too_long"
        try:
            RecordRejectRequest(predefined_reason=reason, comment=note if isinstance(note, str) else None)
        except ValidationError:
            return "other_without_note" if reason == "other" else "invalid_reason"
    return None


def _note(decision: dict) -> Optional[str]:
    note = decision.get(fc.F_NOTE)
    return note.strip() if isinstance(note, str) and note.strip() else None


def _actor(decision: dict) -> str:
    by = decision.get(fc.F_DECIDED_BY)
    name = by.strip() if isinstance(by, str) and by.strip() else ""
    return (f"{ACTOR_PREFIX}: {name}" if name else ACTOR_PREFIX)[:255]


def _apply_one(db: Session, rec, decision: dict) -> str:
    """Move one record to Fisqua's decision. Returns approved, rejected or unchanged; raises HTTPException if refused."""
    from app.api.records import _apply_status_change, reject_locked_record

    actor = _actor(decision)
    reviewer = SimpleNamespace(role="reviewer", username=actor)
    if decision[fc.F_DECISION] == fc.DECISION_APPROVED:
        if rec.status == "approved":
            return "unchanged"
        if rec.status == "rejected":
            _apply_status_change(db, rec, "in_review", reviewer)
        _apply_status_change(db, rec, "approved", reviewer)
        return "approved"

    reason, note = decision[fc.F_REASON], _note(decision)
    if rec.status == "rejected" and rec.rejections:
        latest = max(rec.rejections, key=lambda r: r.id)
        if (latest.predefined_reason, latest.comment, latest.rejected_by) == (reason, note, actor):
            return "unchanged"
    if rec.status in ("approved", "rejected"):
        _apply_status_change(db, rec, "in_review", reviewer)
    reject_locked_record(db, rec, reason, note, actor)
    return "rejected"


def _apply_rotations(rec, decision: dict, report: dict) -> None:
    current = {(img.capture_id, img.role): img for img in rec.images if img.is_current}
    wanted = {}
    rotations = decision.get(fc.F_ROTATIONS) or []
    if not isinstance(rotations, list):
        rotations = []
    for rotation in rotations:
        turns = rotation.get(fc.F_QUARTER_TURNS) if isinstance(rotation, dict) else None
        role = rotation.get(fc.F_ROLE) if isinstance(rotation, dict) else None
        key = (rotation.get(fc.F_CAPTURE_ID), role) if isinstance(rotation, dict) else None
        why = None
        if isinstance(turns, bool) or not isinstance(turns, int) or turns not in fc.QUARTER_TURNS:
            why = "invalid_quarter_turns"
        elif role not in fc.ROTATION_ROLES:
            why = "invalid_role"
        elif key not in current:
            why = "no_such_image"
        if why is not None:
            report["counts"]["rotations_skipped"] += 1
            report["rotation_skips"].append({"captua_record_id": rec.id, "reason": why})
            continue
        wanted[key] = turns
    for key, img in current.items():
        img.rotation_correction_quarter_turns = wanted.get(key)
    report["counts"]["rotations"] += len(wanted)


def _lock_records(db: Session, ids: list) -> dict:
    """{id: record} for the records with these ids, with images and rejections, their rows locked in id order."""
    from app.models.record import Record

    if not ids:
        return {}
    return {rec.id: rec for rec in (db.query(Record)
                                     .options(joinedload(Record.images), joinedload(Record.rejections))
                                     .filter(Record.id.in_(ids))
                                     .order_by(Record.id)
                                     .with_for_update(of=Record)
                                     .all())}


def _empty_report() -> dict:
    return {"counts": {"approved": 0, "rejected": 0, "unchanged": 0, "skipped": 0,
                       "rotations": 0, "rotations_skipped": 0},
            "skipped": [], "rotation_skips": []}


def apply_decisions(db: Session, collection_id: int, body: Any, now: Optional[datetime] = None) -> dict:
    """Apply one decisions set to a volume (module header). Commits. Returns what it did.

    Statuses: "applied", "already_applied", "stale" (for another manifest),
    "invalid" (no decisionsId or no records list), "error" (rolled back).
    """
    from app.core import audit
    from app.models.collection import Collection
    from app.models.fisqua import FisquaStatus
    from app.models.record import Record

    now = now or _now()
    if not isinstance(body, dict) or not isinstance(body.get(fc.F_DECISIONS_ID), str) \
            or not isinstance(body.get(fc.F_RECORDS), list):
        return {"status": "invalid"}
    decisions_id = body[fc.F_DECISIONS_ID][:255]
    status = db.get(FisquaStatus, collection_id)
    if status is None:
        status = FisquaStatus(collection_id=collection_id, status_checked_at=now)
        db.add(status)
    if status.decisions_id == decisions_id:
        db.rollback()
        return {"status": "already_applied", "decisions_id": decisions_id}

    collection = db.get(Collection, collection_id)
    stem = collection.filename_stem if collection is not None else None
    current_row = _current_row(db, collection_id)
    current = current_row[1] if current_row is not None else None
    if current is not None and body.get(fc.F_MANIFEST_ID) == current \
            and _newer_pending(db, collection_id, current_row[0]):
        # A later handover is on its way: the records may already be what
        # it sends, not what this set decided. Not stale, since that
        # handover may yet be withdrawn or refused; read again next tick.
        db.rollback()
        logger.info("Fisqua decisions %s for volume %s wait: a newer handover is not yet delivered",
                    decisions_id, collection_id)
        return {"status": "pending_handover", "decisions_id": decisions_id}
    if current is None or body.get(fc.F_MANIFEST_ID) != current:
        status.decisions_stale_id = decisions_id
        db.commit()
        logger.info("Fisqua decisions %s for volume %s are for manifest %r, not the current %r; not applied",
                    decisions_id, collection_id, body.get(fc.F_MANIFEST_ID), current)
        audit.log_event(db, level="INFO", category="activity", action=STALE_ACTION, subject=stem,
                        detail=f"volume {collection_id} decisions={decisions_id} for another manifest")
        return {"status": "stale", "decisions_id": decisions_id}

    report = _empty_report()
    listed = _listed_captures(db, current_row[0])
    decisions = [d for d in body[fc.F_RECORDS] if isinstance(d, dict)]
    ids = sorted({i for i in (_record_id(d.get(fc.F_CAPTUA_RECORD_ID)) for d in decisions) if i is not None})
    try:
        records = _lock_records(db, ids)
        # Again, under the locks: a promotion may have committed while the
        # locks were awaited. A promotion locks only the volume's captured
        # records, not these, so one can still commit after this check; what
        # it cannot do is change these records' images, since a retake takes
        # the record lock (app.api.cameras, through records._locked_record). The images
        # check below reads the records as locked, so a decision is never
        # applied to images other than those it decided.
        relocked = _current_row(db, collection_id)
        if relocked is None or relocked[0] != current_row[0] or _newer_pending(db, collection_id, current_row[0]):
            db.rollback()
            logger.info("Fisqua decisions %s for volume %s wait: a newer handover came while the "
                        "records were being locked", decisions_id, collection_id)
            return {"status": "pending_handover", "decisions_id": decisions_id}
        current_images = _current_images(db, records.values())

        for decision in decisions:
            raw = decision.get(fc.F_CAPTUA_RECORD_ID)
            rec = records.get(_record_id(raw))

            def skip(why):
                report["counts"]["skipped"] += 1
                report["skipped"].append({"captua_record_id": _shown_id(raw), "reason": why})

            if rec is None or rec.collection_id != collection_id:
                skip("not_in_volume")
                continue
            why = _why_not(decision)
            if why is not None:
                skip(why)
                continue
            if rec.status == "captured":
                skip("captured")
                continue
            # The decision is about the images Fisqua was sent, image by
            # image. A record retaken since, or one that lost, gained or
            # swapped an image of its pair, has others, and the decision is
            # not theirs.
            if rec.id not in listed or current_images[rec.id] != listed[rec.id]:
                skip("images_changed")
                continue
            # Every transition _apply_one takes is in STATUS_TRANSITIONS for a
            # reviewer, from the statuses checked above, so none is refused
            # part-way; the guard is for a table changed later.
            try:
                outcome = _apply_one(db, rec, decision)
            except HTTPException:
                skip("transition_refused")
                continue
            report["counts"][outcome] += 1
            _apply_rotations(rec, decision, report)

        status.decisions_id = decisions_id
        status.decisions_decided_at = _parse_time(body.get(fc.F_DECIDED_AT))
        status.decisions_applied_at = now
        status.decisions_manifest_id = current
        status.decisions_report = report
        status.decisions_stale_id = None
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Applying Fisqua decisions %s to volume %s raised", decisions_id, collection_id)
        return {"status": "error", "decisions_id": decisions_id}

    c = report["counts"]
    audit.log_event(db, level="INFO", category="activity", action=APPLIED_ACTION, subject=stem,
                    detail=(f"volume {collection_id} decisions={decisions_id} approved={c['approved']} "
                            f"rejected={c['rejected']} unchanged={c['unchanged']} skipped={c['skipped']} "
                            f"rotations={c['rotations']} rotations_skipped={c['rotations_skipped']}"))
    return {"status": "applied", "decisions_id": decisions_id, **report}


def decisions_state(status) -> Optional[dict]:
    """The `decisions` object of the volume's fisqua state, or None when no set has been seen."""
    if status is None or (status.decisions_id is None and status.decisions_stale_id is None):
        return None
    report = status.decisions_report or {}
    return {
        "decisions_id": status.decisions_id,
        "decided_at": status.decisions_decided_at,
        "applied_at": status.decisions_applied_at,
        "manifest_id": status.decisions_manifest_id,
        "counts": report.get("counts"),
        "skipped": report.get("skipped", []),
        "rotation_skips": report.get("rotation_skips", []),
        # The last set Fisqua offered was for another manifest, and was not applied.
        "stale": status.decisions_stale_id is not None,
    }
