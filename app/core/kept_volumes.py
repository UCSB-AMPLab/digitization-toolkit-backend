"""Volumes Captua keeps on the unit (NEH-320): collections.kept_on_unit.

Scanner only (`offline`) is a mode in its own right: an institution may
choose it so that its material stays on the unit. A volume created in that
mode is created with `kept_on_unit` set, by both creation paths
(POST /collections/ and the list import); an admin may set it on a new
volume in any mode (in fisqua mode, as part of the admin's override, with
no reference-code check with Fisqua), or on an existing one through
keep(), and only an admin clears it: release() for one volume and the
volumes inside it, or the switch out of offline mode for all of them
(app.core.unit_mode.change, `keptVolumes: include`).

**The invariant.** A volume inside a kept volume is kept. A kept volume
inside one that is not kept is allowed: an admin may keep one sub-volume
as a test. Every path that sets or clears the flag, or changes a volume's
parent, keeps it:

* POST /collections/ (app.api.collections.create_collection): a volume
  created under a kept parent is created kept, the parent's flag read
  again with its row FOR SHARE in the insert's transaction (a keep or a
  release of the parent committed meanwhile is a 409 `mode_changed`). By
  the invariant a kept grandparent means a kept parent, so the direct
  parent is the only one read.
* The list import (app.core.list_import): it creates top-level volumes
  only, which have no parent to follow.
* keep(): keeps the volume and every volume inside it, at any depth.
* release(): refused (`kept_parent`) while the volume's parent is kept;
  otherwise it releases the volume and every kept volume inside it.
* The switch out of offline mode with `include`
  (app.core.unit_mode._decide_kept): releases every kept volume, so none
  is left to break the invariant.
* A re-parent (PATCH /collections/{id}, refuse_kept_reparent): refused
  only when the new parent is kept and the moving volume is not. A kept
  volume may move under a parent that is not kept, or to the top level.
  The moving volume's own sub-volumes keep their place under it, so they
  hold the invariant as they held it before.
* Deletion: DELETE /collections/{id} refuses (409) while the volume
  still holds sub-volumes, and DELETE /projects/{id} while the project
  still holds volumes, so through the API a volume is deleted only once
  nothing is inside it. The foreign key's ondelete=CASCADE is not reached
  from the API.
* No other path writes kept_on_unit or parent_collection_id.

While a volume is kept:

* none of its images is copied offsite: image_upload.pending_work leaves it
  out, and a run re-reads the flag with each object before staging it
  (image_upload._image_state), so a keep committed meanwhile is caught;
* none of its Fisqua outbox rows is promoted or sent: a handover still
  queues its `waiting_for_upload` row (enqueue_handover), and the row waits
  there, neither superseded nor refused (fisqua_outbox._promote);
* it counts as pending nowhere (image_upload.volume_upload_state,
  unit_upload_summary);
* nothing of it can be freed from the card (offsite_free refuses it, code
  `kept_on_unit`), though a kept volume has nothing verified offsite.

The catalogue dump is a pg_dumpall of the whole database
(app.core.catalogue_backup) and cannot hold back one volume: a kept
volume's records go offsite with the daily dump in backup and fisqua mode;
its images stay on the unit.

**Lock order.** Every path here that locks more than one collection row
takes them in ascending id order: refuse_kept_move (after the records'
locks), lock_for_reparent, keep() and release(). A path that takes one
collection row only (image_upload._claim, record FOR UPDATE then its
volume FOR SHARE; the creation paths' parent FOR SHARE) cannot close a
cycle with them. The switch's include (unit_mode._decide_kept) locks
every kept row in ascending id order before its UPDATE, so it takes them
in the same order. The claim is about locked reads: a path may read
collection rows without a lock before it takes its locks (the PATCH reads
a volume's codes in fisqua mode, so that Fisqua is not asked while a row
is held), and a read without a lock waits for no one.

The rows keep(), release() and the include write are locked FOR NO KEY
UPDATE (_lock, `with_for_update(key_share=True)`). That
conflicts with every lock the arguments above rely on: FOR SHARE (the
creation paths' parent, the re-parent's new parent, refuse_kept_move's
source, image_upload._claim's volume), FOR UPDATE (refuse_kept_move's
target, record_sequence.next_sequences, the PATCH's own row) and FOR NO
KEY UPDATE (another keep, release or include). It does not conflict with FOR KEY SHARE, the lock PostgreSQL
takes implicitly on a collection row for the foreign-key check of every
insert or key change of a row that references it: records.collection_id,
record_status_changes.collection_id (POST /records/bulk-status inserts
them in record order, whatever their volumes' ids),
fisqua_outbox.collection_id, fisqua_status.collection_id and
collections.parent_collection_id itself. Those implicit
lockers take collection rows in no particular order, so a FOR UPDATE here
could deadlock with them; FOR NO KEY UPDATE does not wait for them. The
paths that still take FOR UPDATE take one or two rows each, or several
in id order:

* refuse_kept_move's target (the records move, PATCH /records/{id},
  add_record), after the records' locks;
* the PATCH of a volume (_locked_collection_query, or lock_for_reparent
  with a new parent): FOR UPDATE because the PATCH may change
  filename_stem or external_id, both unique-indexed, which a flush would
  otherwise upgrade to FOR UPDATE under the lock it holds;
* volume_stem.locked_volume_query (the capture paths' stem check, after
  their record lock), and its multi-row caller
  recapture_turn.lock_rescan_volumes, which locks the full rescans among
  the volumes in id order after the records' locks.

The Fisqua outbox's promotion (fisqua_outbox._kept_now) takes one volume
row FOR SHARE before it moves a waiting row out of waiting_for_upload, so
a keep and a promotion take turns on the volume's row; one collection row
only, so it closes no cycle with the paths above.

Version: 0.3.3
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

# Problem codes.
KEPT_VOLUMES_DECISION = "kept_volumes_decision"
ALREADY_LEFT_UNIT = "already_left_unit"
ALREADY_KEPT = "already_kept_on_unit"
NOT_KEPT = "not_kept_on_unit"
KEPT_ADMIN_ONLY = "kept_on_unit_admin_only"
KEPT_MOVE = "kept_on_unit_move"
KEPT_PARENT = "kept_parent"
KEPT_CHANGED = "kept_volumes_changed"

KEPT_VOLUMES_DECISION_MESSAGE = (
    "Captua keeps volumes made in Scanner only mode on this unit. Say whether the switch includes "
    "them (keptVolumes: include) or Captua keeps them on the unit (keptVolumes: keep)."
)
ALREADY_LEFT_UNIT_MESSAGE = (
    "Part of this volume, or of a volume inside it, has already left the unit: a file was copied "
    "offsite, or a manifest was sent or is being sent to Fisqua. Captua can no longer keep it on the unit."
)
ALREADY_KEPT_MESSAGE = "Captua already keeps this volume on the unit."
NOT_KEPT_MESSAGE = "Captua does not keep this volume on the unit, so there is nothing to release."
KEPT_ADMIN_ONLY_MESSAGE = "Only an admin can keep a new volume on the unit."
KEPT_REPARENT_MESSAGE = (
    "Captua keeps that parent on the unit, and every volume inside a kept volume is kept too, so a "
    "volume Captua does not keep cannot be moved under it. Keep this volume on the unit first, or "
    "choose a parent that Captua does not keep."
)
KEPT_PARENT_MESSAGE = (
    "Captua keeps the volume that holds this one on the unit, and every volume inside a kept volume is "
    "kept too. Include the volume that holds it in the current mode first: Captua includes the volumes "
    "inside it with it."
)
KEPT_CHANGED_MESSAGE = (
    "The volumes inside this one changed while Captua was reading them. Nothing was changed; try again."
)
KEPT_MOVE_MESSAGE = (
    "Captua keeps one of these volumes on the unit and not the other, so records cannot be moved "
    "between them. Include the kept volume in the current mode first, or move the records to a "
    "volume Captua keeps on the unit too."
)

# How many times keep() and release() read a volume's sub-volumes again when
# they changed between the read and the row locks.
LOCK_ATTEMPTS = 3

# Outbox states that mean nothing of the volume reached Fisqua or is on its way.
_NOT_SENT = ("waiting_for_upload", "superseded", "withdrawn")


class KeepRefused(Exception):
    """A keep or a release that cannot be done: an HTTP status and a {code, message} detail."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message}


def kept_on_create(mode: str, requested: Optional[bool], is_admin: bool) -> bool:
    """Whether a new volume is kept: always in offline mode; else when an admin asks for it.

    Raises KeepRefused (403, kept_on_unit_admin_only) for a non-admin asking
    for it outside offline mode; in offline mode the request is redundant
    and not refused. A false asked for in offline mode is ignored.
    """
    from app.core import unit_mode

    if mode == unit_mode.OFFLINE:
        return True
    if requested and not is_admin:
        raise KeepRefused(403, KEPT_ADMIN_ONLY, KEPT_ADMIN_ONLY_MESSAGE)
    return bool(requested)


def kept_volumes(db: Session) -> list:
    """Every kept volume as {id, name, files}, in id order; `files` is the count Captua would copy offsite.

    Files are counted as image_upload._object_counts counts a volume's
    objects; a volume with no stem has none.
    """
    from app.core.image_upload import _object_counts
    from app.models.collection import Collection

    rows = (db.query(Collection.id, Collection.name)
            .filter(Collection.kept_on_unit.is_(True))
            .order_by(Collection.id).all())
    if not rows:
        return []
    counts = _object_counts(db, [cid for cid, _ in rows])
    return [{"id": cid, "name": name, "files": counts.get(cid, {}).get("total", 0)} for cid, name in rows]


def _lock(db: Session, collection_id: int, *, share: bool = False):
    """The volume's row, read afresh, FOR NO KEY UPDATE (FOR SHARE with `share`); None if it is gone.

    FOR NO KEY UPDATE, not FOR UPDATE: none of these writers changes the
    row's key, and FOR UPDATE would also conflict with the FOR KEY SHARE
    that PostgreSQL takes on the row for each foreign-key check of a row
    that references it (see "Lock order" in the module docstring).
    """
    from app.models.collection import Collection

    query = db.query(Collection).filter(Collection.id == collection_id)
    query = query.with_for_update(read=True) if share else query.with_for_update(key_share=True)
    return query.populate_existing().first()


def _lock_in_id_order(db: Session, ids, *, share=()) -> dict:
    """Lock each of `ids` in ascending id order, those in `share` FOR SHARE, the rest FOR NO KEY UPDATE.

    Returns {id: row or None}. One statement per row, so the order the
    rows are locked in is the order asked for, whatever plan the database
    would choose for a single statement.
    """
    share = set(share)
    return {i: _lock(db, i, share=i in share) for i in sorted(set(ids))}


def _subtree(db: Session, root_id: int) -> dict:
    """The volume and every volume inside it, at any depth: {id: (kept_on_unit, filename_stem, external_id)}.

    Read level by level through parent_collection_id, without locks; a
    malformed cyclic tree is read once round.
    """
    from app.models.collection import Collection

    found: dict = {}
    frontier = [root_id]
    root = (db.query(Collection.id, Collection.kept_on_unit, Collection.filename_stem, Collection.external_id)
            .filter(Collection.id == root_id).first())
    if root is None:
        return found
    found[root.id] = (bool(root.kept_on_unit), root.filename_stem, root.external_id)
    while frontier:
        rows = (db.query(Collection.id, Collection.kept_on_unit, Collection.filename_stem, Collection.external_id)
                .filter(Collection.parent_collection_id.in_(frontier)).all())
        frontier = []
        for row in rows:
            if row.id not in found:
                found[row.id] = (bool(row.kept_on_unit), row.filename_stem, row.external_id)
                frontier.append(row.id)
    return found


def _with_detail(collection_id: int, verb: str, ids: list) -> str:
    """The detail of a keep or a release: the volume, then how many volumes inside it went with it, and their ids.

    Within unit_mode.DETAIL_MAX: when every id does not fit, as many as fit,
    then `…` and the number left out.
    """
    from app.core.unit_mode import DETAIL_MAX

    head = f"volume {collection_id}; {len(ids)} volume(s) inside it {verb} with it"
    if not ids:
        return head
    head += ": ids "
    full = head + ", ".join(str(i) for i in ids)
    if len(full) <= DETAIL_MAX:
        return full
    shown: list = []
    for i in ids:
        tail = f" … {len(ids) - len(shown) - 1} more"
        if len(head + ", ".join(str(x) for x in shown + [i]) + tail) > DETAIL_MAX:
            break
        shown.append(i)
    return head + ", ".join(str(x) for x in shown) + f" … {len(ids) - len(shown)} more"


def _has_left_unit(db: Session, collection) -> bool:
    """Whether a file of the volume left or may be leaving the unit, or a manifest left or is leaving.

    A file counts from its first offsite row that is not refused: pending
    (its key claimed, the copy under way or left by a crash), uploaded or
    verified. A row whose image was deleted (record_image_id NULL) is
    counted when its key is under the volume's `<tenant>/<stem>/`, the only
    way it can be told to be this volume's; with no stem or no valid tenant
    such a row cannot be attributed, and is not counted.
    """
    from app.core.image_upload import configured_tenant_or_none
    from app.models.fisqua import FisquaOutbox
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    collection_id = collection.id
    copied = (db.query(OffsiteObject.id)
              .join(RecordImage, OffsiteObject.record_image_id == RecordImage.id)
              .join(Record, RecordImage.record_id == Record.id)
              .filter(Record.collection_id == collection_id, OffsiteObject.refused_at.is_(None))
              .first())
    if copied is not None:
        return True
    tenant = configured_tenant_or_none()
    if tenant and collection.filename_stem:
        prefix = f"{tenant}/{collection.filename_stem}/"
        orphan = (db.query(OffsiteObject.id)
                  .filter(OffsiteObject.record_image_id.is_(None), OffsiteObject.refused_at.is_(None),
                          func.substr(OffsiteObject.object_key, 1, len(prefix)) == prefix)
                  .first())
        if orphan is not None:
            return True
    sent = (db.query(FisquaOutbox.id)
            .filter(FisquaOutbox.collection_id == collection_id, FisquaOutbox.state.notin_(_NOT_SENT))
            .first())
    return sent is not None


def keep(db: Session, collection_id: int, actor: Optional[str]):
    """Keep an existing volume on the unit, and every volume inside it; commits. Returns the volume, or None if not found.

    The volumes inside it at any depth are kept with it; those already kept
    are left as they are. Logged as one `volume_kept_on_unit` entry for
    the volume, whose detail says how many volumes inside it were kept with
    it, and their ids (_with_detail).

    Refused as a whole, with nothing changed (KeepRefused 409), when the
    volume is already kept (already_kept_on_unit), and when anything of it
    or of any volume inside it has left or may be leaving the unit
    (already_left_unit, _has_left_unit): a file with an offsite row that is
    not refused, or an outbox row in any state but waiting_for_upload,
    superseded or withdrawn. Their unsent rows stay waiting.

    Locks: the volume and every volume inside it FOR NO KEY UPDATE, in one
    pass in ascending id order (_lock_in_id_order), checked under those
    locks. The
    set of volumes inside it is read before the locks, so it is read again
    under them; when it changed (a volume created under one of them, or
    moved in or out, committed in between) the locks are let go and the
    whole keep starts again, at most LOCK_ATTEMPTS times, then a 409
    kept_volumes_changed. Once they are all held the set cannot change: a
    creation under any of them takes its parent FOR SHARE, and a re-parent
    takes the moving volume FOR UPDATE and its new parent FOR SHARE, all of
    which conflict with FOR NO KEY UPDATE.

    The order cannot invert against the other lockers of collection rows:
    refuse_kept_move, lock_for_reparent and the switch's include
    (unit_mode._decide_kept) take theirs in ascending id order too
    (refuse_kept_move after the records', and keep takes no record lock);
    image_upload._claim takes a record FOR UPDATE and then one volume FOR
    SHARE, and fisqua_outbox._kept_now one volume FOR SHARE, one collection
    row each, so with keep holding no record lock
    no cycle can close. The uploader's claim and a keep take turns on the
    volume's row: a claim committed first is a pending row here, and a keep
    committed first changes the image state the claim compares, so it
    writes nothing.
    """
    from app.core import unit_mode

    for _ in range(LOCK_ATTEMPTS):
        tree = _subtree(db, collection_id)
        if not tree:
            db.rollback()
            return None
        locked = _lock_in_id_order(db, tree)
        collection = locked.get(collection_id)
        if collection is None:
            db.rollback()
            return None
        if set(_subtree(db, collection_id)) != set(tree):
            db.rollback()
            continue
        if collection.kept_on_unit:
            db.rollback()
            raise KeepRefused(409, ALREADY_KEPT, ALREADY_KEPT_MESSAGE)
        volumes = [locked[i] for i in sorted(tree)]
        if any(_has_left_unit(db, volume) for volume in volumes):
            db.rollback()
            raise KeepRefused(409, ALREADY_LEFT_UNIT, ALREADY_LEFT_UNIT_MESSAGE)
        with_it = [v.id for v in volumes if v.id != collection_id and not v.kept_on_unit]
        for volume in volumes:
            volume.kept_on_unit = True
        unit_mode._log_in_session(db, "volume_kept_on_unit", _with_detail(collection_id, "kept", with_it),
                                  actor=actor, subject=collection.name)
        db.commit()
        return collection
    raise KeepRefused(409, KEPT_CHANGED, KEPT_CHANGED_MESSAGE)


def release(db: Session, collection_id: int, actor: Optional[str], *, check=None):
    """Release a kept volume, and every kept volume inside it; commits. Returns the volume, or None if not found.

    Refused (KeepRefused 409) when the volume is not kept (not_kept_on_unit)
    and when its parent volume is kept (kept_parent): a volume inside a kept
    volume is kept, so the parent is released first, and this one with it.
    Logged as one `volume_released_from_unit` entry for the volume, whose
    detail says how many volumes inside it were released with it, and their
    ids (_with_detail).

    `check`, when given, is called with the stem and external_id of each
    volume it will release, in id order, before any flag is cleared and
    outside the row locks (the caller's Fisqua check in fisqua mode);
    whatever it raises refuses the whole release, with every flag left set.
    When a volume's codes changed between the check and the locks, the
    release starts again and checks them again. Once released the volumes
    are copied offsite and, in fisqua mode, sent like any other.

    Locks: the parent FOR SHARE (so a keep of it waits, or commits first and
    is seen here), the volume and every volume inside it FOR NO KEY UPDATE, all in
    one pass in ascending id order (_lock_in_id_order); the parent is not
    one of the others, so each row is locked once, in one mode. The set of
    volumes inside it, and the parent, are read before the locks and read
    again under them; when either changed the locks are let go and the
    whole release starts again, at most LOCK_ATTEMPTS times, then a 409
    kept_volumes_changed. Why the order cannot invert against
    refuse_kept_move, lock_for_reparent and image_upload._claim: as for
    keep(). The switch out of offline mode (app.core.unit_mode._decide_kept)
    locks every kept row FOR NO KEY UPDATE in ascending id order before it
    releases them, the same order as here.
    """
    from app.core import unit_mode
    from app.models.collection import Collection

    for _ in range(LOCK_ATTEMPTS):
        stored = (db.query(Collection.kept_on_unit, Collection.parent_collection_id)
                  .filter(Collection.id == collection_id).first())
        if stored is None:
            db.rollback()
            return None
        if not stored.kept_on_unit:
            db.rollback()
            raise KeepRefused(409, NOT_KEPT, NOT_KEPT_MESSAGE)
        parent_id = stored.parent_collection_id
        if parent_id is not None and bool(db.query(Collection.kept_on_unit)
                                          .filter(Collection.id == parent_id).scalar()):
            db.rollback()
            raise KeepRefused(409, KEPT_PARENT, KEPT_PARENT_MESSAGE)
        tree = _subtree(db, collection_id)
        db.rollback()
        to_release = {i: codes[1:] for i, codes in tree.items() if codes[0]}
        if check is not None:
            for i in sorted(to_release):
                check(*to_release[i])

        ids = set(tree) | ({parent_id} if parent_id is not None else set())
        locked = _lock_in_id_order(db, ids, share={parent_id} if parent_id is not None else ())
        collection = locked.get(collection_id)
        if collection is None:
            db.rollback()
            return None
        if not collection.kept_on_unit:
            db.rollback()
            raise KeepRefused(409, NOT_KEPT, NOT_KEPT_MESSAGE)
        now = _subtree(db, collection_id)
        if collection.parent_collection_id != parent_id or set(now) != set(tree) or (
                {i: codes[1:] for i, codes in now.items() if codes[0]} != to_release):
            # A parent, a volume inside it or a code changed since the read: rare.
            db.rollback()
            continue
        parent = locked.get(parent_id) if parent_id is not None else None
        if parent is not None and parent.kept_on_unit:
            db.rollback()
            raise KeepRefused(409, KEPT_PARENT, KEPT_PARENT_MESSAGE)
        for i in to_release:
            locked[i].kept_on_unit = False
        with_it = sorted(i for i in to_release if i != collection_id)
        unit_mode._log_in_session(db, "volume_released_from_unit", _with_detail(collection_id, "released", with_it),
                                  actor=actor, subject=collection.name)
        db.commit()
        return collection
    raise KeepRefused(409, KEPT_CHANGED, KEPT_CHANGED_MESSAGE)


def refuse_kept_move(db: Session, from_collection_id: Optional[int], to_collection_id: Optional[int]) -> None:
    """KeepRefused (409, kept_on_unit_move) for a move of records between a kept volume and one that is not.

    A record outside every volume counts as not kept, so a record taken out
    of a kept volume, or put into one from outside every volume, is refused
    too. A move within one volume, or between two kept (or two not kept)
    volumes, passes. Every path that changes a record's volume calls it,
    after its record locks: POST /collections/{id}/move-records,
    PATCH /records/{id} and POST /projects/{id}/add_record/{rec_id}. A volume's own re-parent is
    refuse_kept_reparent's.

    The flags are read under the volumes' row locks, taken in id order after
    the records' (record before volume, as on every path that takes both):
    the source FOR SHARE, so a keep or a release of it (FOR NO KEY UPDATE) waits for
    the move's commit or commits before this read; the target FOR UPDATE,
    the lock record_sequence.next_sequences takes on it next in the same
    transaction, so the move never holds a share lock it then has to
    upgrade, which two moves into one volume could deadlock on.
    """
    from app.models.collection import Collection

    if from_collection_id == to_collection_id:
        return
    flags = {}
    for collection_id in sorted(i for i in (from_collection_id, to_collection_id) if i is not None):
        query = db.query(Collection.kept_on_unit).filter(Collection.id == collection_id)
        query = query.with_for_update() if collection_id == to_collection_id else query.with_for_update(read=True)
        flags[collection_id] = bool(query.scalar())

    def kept(collection_id: Optional[int]) -> bool:
        return flags.get(collection_id, False) if collection_id is not None else False

    if kept(from_collection_id) != kept(to_collection_id):
        raise KeepRefused(409, KEPT_MOVE, KEPT_MOVE_MESSAGE)


def lock_for_reparent(db: Session, collection_id: int, new_parent_id: Optional[int]):
    """The PATCH's row locks: the volume FOR UPDATE and its new parent FOR SHARE, in ascending id order.

    FOR UPDATE on the volume, not FOR NO KEY UPDATE: the PATCH may change
    its filename_stem or external_id, both unique-indexed, and a flush
    changing them would upgrade the lock to FOR UPDATE while the PATCH
    holds it. Returns the volume (None if not found), read afresh under
    its lock. With
    no new parent, or a new parent that is the volume itself (refused later
    by the PATCH), the volume alone is locked. Taken before the PATCH reads
    any other collection row under a lock (its read of the codes in fisqua
    mode, before this, takes none), so the PATCH never holds the
    higher of the two while it waits for the lower: refuse_kept_move,
    keep() and release() lock in the same ascending order, and
    image_upload._claim and fisqua_outbox._kept_now lock one collection
    row only, so none of them can close a cycle with it.
    """
    from app.models.collection import Collection

    shares = () if new_parent_id is None or new_parent_id == collection_id else (new_parent_id,)
    locked = {}
    for i in sorted({collection_id, *shares}):
        if i == collection_id:
            locked[i] = (db.query(Collection).filter(Collection.id == i).with_for_update()
                         .populate_existing().first())
        else:
            locked[i] = _lock(db, i, share=True)
    return locked[collection_id]


def refuse_kept_reparent(db: Session, collection, new_parent_id: Optional[int]) -> None:
    """KeepRefused (409, kept_on_unit_move) for a re-parent that would put a volume not kept inside a kept one.

    `collection` is the moving volume; the caller holds its row FOR UPDATE
    and the new parent's FOR SHARE (lock_for_reparent), so a keep or a
    release of either waits for the PATCH's commit or commits before this
    read. Refused only when the new parent is kept and the moving volume is
    not (the invariant in the module docstring). A kept volume may move
    under a parent not kept, or to the top level of a project (no parent);
    a volume not kept may move under any parent not kept.
    """
    from app.models.collection import Collection

    if new_parent_id is None or collection.kept_on_unit:
        return
    parent_kept = bool(db.query(Collection.kept_on_unit).filter(Collection.id == new_parent_id).scalar())
    if parent_kept:
        raise KeepRefused(409, KEPT_MOVE, KEPT_REPARENT_MESSAGE)
