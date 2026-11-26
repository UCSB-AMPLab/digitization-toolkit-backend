"""An unfinished adoption of captures, reported to administrators and repaired from the unit.

An adoption whose journal is still in the data directory
(app.core.adopt_journal) holds both volumes it names: the journal guards
refuse every capture, free, restore, rebuild, export, delete and record or
image move on them. report answers GET /system/adoption with what the
journal names and why it is still there; repair answers
POST /system/adoption/repair by fetching the files the recovery lacks and
running that recovery again.

Running now
-----------
`running` answers what holds ADOPTION_LOCK, from adopt_journal.running: the
snapshot an adoption, a recovery or a repair publishes right after it takes
the lock and withdraws right before it releases it. The report reads that
snapshot and never takes the lock; an adoption is in it from the moment it
takes the lock, before its journal exists. A storage switch holds the lock
and is not in it.

Running or left
---------------
ADOPTION_LOCK is held by an adoption from before its journal is created to
after it is removed or kept, and by every recovery, in the one backend
process the unit runs. A journal read while the lock is held, or one that
changed between two reads around the look at the lock, belongs to a run in
progress: `state: "recovering"`. A journal read twice with the same run_id
while the lock was free between the reads was left by a run that has ended
(a dead process, or an adoption here that stopped and kept it): `state:
"blocked"`. Startup recovery runs before the backend serves, so after
start a journal with the lock free is one that recovery, or the adoption's
own undo, could not clear.

The reason
----------
For a blocked journal the reason is worked out now, as the recovery would:
the row checks of adopt_journal.decide, then the files of `moves` in order
(the first at both paths or at neither). A journal that passes both carries
the code of the last recovery in this process when that recovery read this
journal and stopped (`recovery_failed`, for one that raised), and
`recovery_not_run` otherwise: an adoption here stopped and kept it, and no
recovery has run since.

Missing files
-------------
`missing` is every move whose file is at neither path, from the journal and
the disk, read with plain SELECTs (no row lock, no write). Each is
repairable or says why not:

* a master or a RAW master's preview: repairable when an offsite_objects
  row of that kind, verified, names the image as its own (record_image_id)
  or as the image it was retired from (retired_image_id); its key and SHA1
  are the row's. A shot or its preview: the same in colour_card_objects
  (kinds `shot`, `preview`; shot_id or retired_shot_id), with a SHA1.
  Without one, `no_verified_object`.
* a raw sensor buffer beside a JPEG master (kinds `buffer`,
  `colour_card_buffer`): never uploaded, `never_uploaded`.
* a thumbnail: derived again (app.core.thumbnail.generate_thumbnail)
  from its master, or a RAW master's preview, and only from bytes checked
  against a digest the records hold: the copy the repair fetches, or the
  copy on disk, hashed at repair time against its verified offsite row's
  SHA1 (and the manifests' sha256 where they give one). A source at
  neither path that cannot be fetched is `thumbnail_source_missing`; one
  with no verified row, or one at both paths, `thumbnail_source_unverified`.
* any file while the records decide nothing (`records_split`):
  `undecided`.

A file at both its old and its new path (`at_both` in the report) makes
the whole journal unrepairable from the unit: recovery stops on it whatever
is placed, so the repair refuses (`repair_not_possible`) and places nothing.

Every lookup is through indexes built once per report or repair: the moves
by kind and image or shot, each file's place (two lexists per move), and the
rows read in chunks of 500 ids, so the work grows with the journal, never
with its square.

Each says whether its bytes can come only from the bucket
(`needs_storage`: a master, a preview, a shot, a shot's preview).

Availability
------------
precheck makes every check the repair makes before its first write, in the
order of REFUSALS, and returns the first refusal or none: a repair or an
adoption running, no journal, a journal unreadable or malformed, its root
not the active one, the active storage unable to take writes
(storage_override.refuse_writes_under_unusable_root), a file at both paths,
a missing file not repairable, no read remote while a file must be fetched
(with nothing to fetch the repair needs none and runs recovery), staging on
another file system, and a volume held by an export, a copy or a read. The
repair refuses on its answer; the report's `repair_available` is exactly
"no refusal" and `repair_refusal` names the one found. Nothing else
computes availability. The checks that happen after the first write are
the repair's own: the copy, the digests, a thumbnail's source hashed, a
destination taken meanwhile.

Repair
------
Under a lock of its own (a second repair is refused, `repair_running`) and
ADOPTION_LOCK, taken without waiting (`adoption_running` while an adoption
or a recovery holds it), and with both volumes held for file changes
(export_jobs.hold_for_file_changes, as the adoption holds them, so the
offsite upload reads neither): each missing file is copied from the bucket
through the read remote (offsite_free._copy_result, the copy restore makes)
into the staging directory, `<projects root>/.captua-adoption-repair/`
(staging_dir), flat and in no volume, under the projects root in use now,
never a path taken from the journal: computed once per repair and used by
every step and every cleanup. The name belongs to the repair by itself (no
volume's stem and no project's directory can take it): the sweep removes
whatever is there, a directory whole (a symbolic link inside it is removed,
never followed), a symbolic link or a file at the name unlinked. The repair
sweeps the name, then creates the directory; a sweep that cannot clear it
refuses the repair (`repair_write_failed`). Before
anything is fetched, the staging directory and every destination must be on
one file system (st_dev), since the last step is a rename; otherwise
`repair_not_possible` (`staging_on_another_filesystem`), and the report's
`staging.same_filesystem` and `repair_available` are false. Each file has its SHA1 checked against the row and
its sha256 against the shot's row or the manifests' fixity index where
either records one, and fsynced. Thumbnails are written by
generate_thumbnail into the same directory. Only when every file has been
fetched and verified are they renamed to the path the records decide (all
records in the target: the new path; all in the source: the old path), each
destination checked to be free first, and their directories fsynced; those
renames are the repair's only writes into a volume. A copy that finds no
object (`repair_object_missing`), a digest that differs
(`repair_digest_mismatch`), or any other failure before the renames
removes the staging directory and changes nothing; the journal stays.
Whatever is at the staging name is removed at the start of every repair and by
startup recovery, with or without a journal (adopt_journal.
recover_adoption_at_startup), under the current root only, so what a killed repair staged never
outlives the next repair or start. A volume's stem cannot begin with a dot
and a project's directory name has its leading dots stripped, so no volume
or project takes its name; the integrity check passes over it as it passes
over the offsite upload's staging directory.

A repair is possible only while the journal's files are on the storage in
use: adopt_journal.journal_projects_root reads the journal's root (its
target root's parent, a string holding an absolute path; anything else is
`journal_unreadable`), and journal_root_is_active compares it, resolved,
with the current projects root. Otherwise the report says `blocked`,
`storage_not_active`, with the root as text (`journal_root`), and the
repair refuses with that reason. adopt_journal.storage_switch_guard allows
the one switch that leads back to that root. Any field of a readable
journal the report or the repair cannot read (_analyse) is reported as
`journal_unreadable`, never raised out of the routes.

With `run_id` (the report's `unfinished.run_id`, the journal's own run
id), a journal in place with another run id, or none, or one that cannot
be read, is refused at once with `journal_changed`, once both locks are
taken, and nothing is repaired. Every refusal names the volumes of the
journal read (`source` and `target`, each {id, name, filename_stem}, None
when there is no readable journal), and so does the answer.

The journal is then checked to be the one read (adopt_journal._require_own)
and the startup recovery run under the same lock
(adopt_journal.recover_holding_lock). Its result is the answer. Files
fetched before a recovery that still stops stay where they were placed:
they are correct copies at the names the records decide. The repair is
written to the system log as `volume_captures_adoption_repaired` (WARN).

Version: 0.3.3
"""

import logging
import os
import secrets
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core import adopt_journal

logger = logging.getLogger(__name__)

CATEGORY = "system"
REPAIRED_ACTION = "volume_captures_adoption_repaired"
NOT_RUN = "recovery_not_run"
STORAGE_NOT_ACTIVE = "storage_not_active"
RECOVERING = "recovering"

MESSAGES = {
    adopt_journal.UNREADABLE: "The adoption's journal cannot be read, so it is not known which volumes it names.",
    adopt_journal.FAILED: "The last recovery of this adoption stopped with an error, which the system log names.",
    adopt_journal.RECORDS_SPLIT: ("The adoption's records are partly in one volume and partly in the other, so it "
                                  "is not known whether it landed."),
    adopt_journal.IMAGE_ROWS: "The rows of some images contradict which volume the adoption's records are in.",
    adopt_journal.CARD_ROWS: ("The rows of some colour card shots or sets contradict which volume the adoption's "
                              "records are in."),
    adopt_journal.AT_BOTH: "A file of the adoption is at both its old and its new path.",
    adopt_journal.AT_NEITHER: "Files of the adoption are at neither their old nor their new path.",
    NOT_RUN: "An adoption stopped and kept its journal, and no recovery has run since.",
    "storage_not_active": ("The adoption's files are on a storage that is not the active one. Activate that storage "
                           "to repair it."),
    RECOVERING: "An adoption of captures, or its recovery, is running now.",
}

NO_JOURNAL_MESSAGE = "No adoption of captures is unfinished on this unit."
UNAVAILABLE_MESSAGE = ("This unit has no key that can read the bucket, so it cannot fetch the missing files. "
                       "Someone with access to the unit must repair the adoption.")
NOT_POSSIBLE_MESSAGE = "Some files the adoption lacks cannot be brought back from the unit, so nothing was changed."
UNREADABLE_MESSAGE = "The adoption's journal cannot be read, so nothing can be repaired from the unit."
ADOPTION_RUNNING_MESSAGE = "An adoption of captures, or its recovery, is running now. Try again when it has ended."
REPAIR_RUNNING_MESSAGE = "A repair of the adoption is running now."
HELD_MESSAGE = "An export or a copy of one of these volumes is running. Wait for it to finish, then try again."
OBJECT_MISSING_MESSAGE = "A file the adoption lacks is not in the bucket under its recorded key. Nothing was changed."
DIGEST_MESSAGE = ("A file fetched from the bucket does not have the digest recorded for it. Nothing was changed.")
COPY_FAILED_MESSAGE = "A file could not be fetched from the bucket. Nothing was changed; try again."
THUMBNAIL_MESSAGE = "A thumbnail could not be made again from its master. Nothing was changed."
WRITE_MESSAGE = "A fetched file could not be written. Nothing was changed."
TAKEN_MESSAGE = "A file has appeared where the repair was to place one. Nothing was changed."
OTHER_FILESYSTEM = "staging_on_another_filesystem"
STORAGE_UNUSABLE = "storage_unusable"
NOT_REPAIRABLE = "files_not_repairable"
NO_READ_REMOTE = "no_read_remote"
VOLUME_HELD = "volume_held"
NOT_ACTIVE_MESSAGE = ("The adoption's files are on a storage that is not the active one. Activate that storage to "
                      "repair it. Nothing was changed.")
UNUSABLE_MESSAGE = ("The active storage cannot take writes now (a drive not connected or not mounted, say), so "
                    "nothing was changed. Connect the drive, then try again.")
OTHER_FILESYSTEM_MESSAGE = ("The repair's staging directory is not on the file system the adoption's files are on, so "
                            "they cannot be moved into place. Nothing was changed.")
AT_BOTH_MESSAGE = ("A file of the adoption is at both its old and its new path, and recovery stops on it whatever "
                   "is placed, so nothing was changed. Someone with access to the unit must decide which copy stays.")
CHANGED_MESSAGE = "The adoption's journal changed during the repair. Nothing more was changed."
RUN_CHANGED_MESSAGE = ("The unfinished adoption on the unit is not the one the request names, so nothing was "
                       "repaired. Read the report again.")
KEPT_MESSAGE = ("The recovery still stopped. The fetched files stay where they were placed: they are correct copies "
                "at the names the records decide.")

# One repair at a time in this process.
_REPAIR_LOCK = threading.Lock()

_PAGE_KINDS = {"master": "capture", "preview": "preview"}
_CARD_KINDS = {"colour_card_shot": "shot", "colour_card_preview": "preview"}
_BUFFER_KINDS = ("buffer", "colour_card_buffer")


class RepairRefused(Exception):
    """A repair that changed nothing; the route answers status_code with as_detail()."""

    def __init__(self, status_code: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


@dataclass
class _Missing:
    kind: str
    old: Path
    new: Path
    id_key: str             # "image_id" or "shot_id"
    owner: Optional[int]
    why_not: Optional[str] = None
    key: Optional[str] = None
    sha1: Optional[str] = None
    sha256: Optional[str] = None
    capture_id: Optional[str] = None
    source: Optional[tuple] = field(default=None)   # a thumbnail's (kind, owner) of the move it is made from
    dest: Optional[Path] = None                     # where the repair places it: the side the records decide
    source_path: Optional[Path] = None              # a thumbnail's source on disk, checked against source_sha1
    source_sha1: Optional[str] = None

    @property
    def repairable(self) -> bool:
        return self.why_not is None

    @property
    def needs_storage(self) -> bool:
        """Whether its bytes can come only from the bucket: a master, a preview, a shot or a shot's preview."""
        return self.kind in _PAGE_KINDS or self.kind in _CARD_KINDS

    def as_dict(self) -> dict:
        return {"kind": self.kind, "old_path": str(self.old), "new_path": str(self.new), "name": self.old.name,
                self.id_key: self.owner, "repairable": self.repairable, "why_not": self.why_not,
                "needs_storage": self.needs_storage}


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _volume(db: Session, collection_id: Optional[int]) -> Optional[dict]:
    from app.models.collection import Collection

    if collection_id is None:
        return None
    row = db.query(Collection.id, Collection.name, Collection.filename_stem).filter(
        Collection.id == collection_id).first()
    return {"id": row[0], "name": row[1], "filename_stem": row[2]} if row else None


def _read_settled():
    """(journal or None, running, unreadable error or None), telling a run in progress from one that ended (module header)."""
    def read():
        try:
            return adopt_journal.read_journal(), None
        except adopt_journal.JournalUnreadable as exc:
            return None, exc

    first, first_error = read()
    running = adopt_journal.ADOPTION_LOCK.locked()
    second, second_error = read()
    if second is None and second_error is None:
        return None, False, None
    same = (first_error is not None and second_error is not None) or (
        first is not None and second is not None and first.get("run_id") == second.get("run_id"))
    return second, running or not same, second_error


def report(db: Session) -> dict:
    """GET /system/adoption (module header): {"unfinished": null, "running": ...} or what the journal names and why.

    `running` is what holds ADOPTION_LOCK now (_running), null when nothing
    publishes itself there; `unfinished.run_id` is the journal's own run id.

    `repair_available` is exactly "precheck found no refusal", and
    `repair_refusal` is the refusal it found: the report and the repair ask
    the same function.
    """
    now_running = _running(db)
    journal, running, unreadable = _read_settled()
    if journal is None and unreadable is None:
        return {"unfinished": None, "running": now_running}
    checked = precheck(db, None if unreadable is not None else journal, repair_running=_REPAIR_LOCK.locked(),
                       adoption_running=running)
    body = {"source": None, "target": None}
    if journal is not None:
        try:
            body["source"], body["target"] = _volume(db, journal.get("source_id")), _volume(db, journal.get("target_id"))
        except Exception:  # noqa: BLE001 - a malformed id names no volume
            db.rollback()
        if isinstance(journal.get("started_at"), str):
            body["started_at"] = journal["started_at"]
    body["run_id"] = journal.get("run_id") if journal is not None and isinstance(journal.get("run_id"), str) \
        else None
    if running:
        reason = RECOVERING
    elif checked.refusal in (adopt_journal.UNREADABLE, STORAGE_NOT_ACTIVE):
        reason = checked.refusal
    else:
        reason = (checked.problem[0] if checked.problem else _file_problem(checked.moves, checked.where)) or \
            _last_code(journal)
    body.update(state="recovering" if running else "blocked", reason=reason,
                message=MESSAGES.get(reason, MESSAGES[NOT_RUN]),
                missing=[m.as_dict() for m in checked.missing], at_both=checked.at_both,
                staging={"path": str(checked.staging), "same_filesystem": checked.same_filesystem},
                repair_refusal=checked.refusal, repair_available=checked.refusal is None)
    if checked.refusal == STORAGE_NOT_ACTIVE:
        body["journal_root"] = str(adopt_journal.journal_projects_root(journal))
    return {"unfinished": body, "running": now_running}


def _running(db: Session) -> Optional[dict]:
    """What holds ADOPTION_LOCK now, as it published itself (adopt_journal.running), with its volumes named.

    Read from the published snapshot alone: the lock is never taken, and
    the volumes are named with plain SELECTs.
    """
    run = adopt_journal.running()
    if run is None:
        return None
    try:
        source, target = _volume(db, run.get("source_id")), _volume(db, run.get("target_id"))
    except Exception:  # noqa: BLE001 - a malformed id from a journal names no volume
        db.rollback()
        source = target = None
    return {"request_id": run.get("request_id"), "kind": run.get("kind"), "started_at": run.get("started_at"),
            "source": source, "target": target}


def _last_code(journal: dict) -> str:
    last = adopt_journal.last_recovery()
    if last and last.get("run_id") == journal.get("run_id") and last.get("outcome") == "unfinished" \
            and last.get("code"):
        return last["code"]
    return NOT_RUN


class _Malformed(Exception):
    """A field of a readable journal that the report or the repair reads is not what the adoption writes."""


def _analyse(db: Session, journal: dict) -> tuple:
    """(landed, problem, moves, where, missing, at_both) of the journal, read-only; _Malformed for a field it cannot read."""
    try:
        landed, problem = adopt_journal.decide(db, journal)
        moves = journal.get("moves") or []
        where = _where(moves)
        missing = _missing(db, journal, landed, where)
        return landed, problem, moves, where, missing, _at_both(moves, where)
    except Exception as exc:  # noqa: BLE001 - any malformed field: a type, a key, a value the database refuses
        db.rollback()
        logger.error(f"Adoption repair: the journal {adopt_journal.journal_file()} cannot be read as an adoption's "
                     f"journal: {exc!r}")
        raise _Malformed(str(exc)) from exc


# Every refusal precheck can return, in the order it checks them: (HTTP status, code, message).
REFUSALS = {
    "repair_running": (409, "repair_running", REPAIR_RUNNING_MESSAGE),
    "adoption_running": (409, "adoption_running", ADOPTION_RUNNING_MESSAGE),
    "no_unfinished_adoption": (409, "no_unfinished_adoption", NO_JOURNAL_MESSAGE),
    adopt_journal.UNREADABLE: (409, "repair_not_possible", UNREADABLE_MESSAGE),
    STORAGE_NOT_ACTIVE: (409, "repair_not_possible", NOT_ACTIVE_MESSAGE),
    STORAGE_UNUSABLE: (409, "repair_not_possible", UNUSABLE_MESSAGE),
    adopt_journal.AT_BOTH: (409, "repair_not_possible", AT_BOTH_MESSAGE),
    NOT_REPAIRABLE: (409, "repair_not_possible", NOT_POSSIBLE_MESSAGE),
    NO_READ_REMOTE: (409, "repair_unavailable", UNAVAILABLE_MESSAGE),
    OTHER_FILESYSTEM: (409, "repair_not_possible", OTHER_FILESYSTEM_MESSAGE),
    VOLUME_HELD: (409, "volume_held", HELD_MESSAGE),
}


@dataclass
class _Checked:
    """What precheck read, and the first refusal it found (None: the repair may go ahead)."""
    refusal: Optional[str] = None
    extra: dict = field(default_factory=dict)
    landed: Optional[bool] = None
    problem: Optional[tuple] = None
    moves: list = field(default_factory=list)
    where: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    at_both: list = field(default_factory=list)
    staging: Optional[Path] = None
    same_filesystem: bool = True
    source: Optional[dict] = None
    target: Optional[dict] = None
    volume_ids: list = field(default_factory=list)

    def refuse(self, reason: str, **extra) -> "_Checked":
        self.refusal, self.extra = reason, extra
        return self

    def raise_refusal(self) -> None:
        status, code, message = REFUSALS[self.refusal]
        raise RepairRefused(status, code, message, reason=self.refusal, **self.extra)


NO_JOURNAL = object()   # precheck's journal argument when there is none


def precheck(db: Session, journal, *, repair_running: bool, adoption_running: bool) -> _Checked:
    """Every check the repair makes before its first write, in order; the first refusal, or none (module header).

    `journal` is the journal read, None when it exists and cannot be read,
    or NO_JOURNAL. Read-only throughout: plain SELECTs and a rollback, stat
    and lexists, storage_override.refuse_writes_under_unusable_root (which
    stats the root), and export_jobs.file_changes_refusal (which reads the
    hold state under its lock and takes no hold). The two running checks are
    the caller's: the repair passes whether it failed to take _REPAIR_LOCK
    and ADOPTION_LOCK without waiting, the report whether they are held now
    (Lock.locked(), which takes neither).
    """
    from app.core.export_jobs import export_jobs
    from app.core.offsite_free import restore_available
    from app.core.storage_override import StorageUnavailableError, refuse_writes_under_unusable_root

    checked = _Checked(staging=staging_dir())
    if repair_running:
        return checked.refuse("repair_running")
    if adoption_running:
        return checked.refuse("adoption_running")
    if journal is NO_JOURNAL:
        return checked.refuse("no_unfinished_adoption")
    if journal is None or adopt_journal.journal_projects_root(journal) is None:
        return checked.refuse(adopt_journal.UNREADABLE, missing=[])
    try:
        checked.source = _volume(db, journal.get("source_id"))
        checked.target = _volume(db, journal.get("target_id"))
        checked.landed, checked.problem, checked.moves, checked.where, checked.missing, checked.at_both = \
            _analyse(db, journal)
    except Exception:  # noqa: BLE001 - any malformed field (_Malformed, or a volume id the database refuses)
        db.rollback()
        return checked.refuse(adopt_journal.UNREADABLE, missing=[])
    checked.volume_ids = sorted(v["id"] for v in (checked.source, checked.target) if v is not None)
    if not adopt_journal.journal_root_is_active(journal):
        return checked.refuse(STORAGE_NOT_ACTIVE, missing=[],
                              journal_root=str(adopt_journal.journal_projects_root(journal)))
    destinations = [m.new if checked.landed else m.old for m in checked.missing] if checked.landed is not None \
        else []
    try:
        refuse_writes_under_unusable_root([checked.staging] + destinations)
    except StorageUnavailableError as exc:
        return checked.refuse(STORAGE_UNUSABLE, missing=[], problem=str(exc))
    if checked.at_both:
        return checked.refuse(adopt_journal.AT_BOTH, missing=[], at_both=checked.at_both)
    lacking = [m.as_dict() for m in checked.missing if not m.repairable]
    if lacking:
        return checked.refuse(NOT_REPAIRABLE, missing=lacking)
    if not _available(checked.missing, restore_available()):
        return checked.refuse(NO_READ_REMOTE, missing=[m.as_dict() for m in checked.missing if m.needs_storage])
    checked.same_filesystem = _same_filesystem(checked.staging, checked.missing, checked.landed)
    if not checked.same_filesystem:
        return checked.refuse(OTHER_FILESYSTEM, staging=str(checked.staging))
    held = export_jobs.file_changes_refusal(checked.volume_ids, HELD_MESSAGE)
    if held is not None:
        return checked.refuse(VOLUME_HELD, detail=held)
    return checked


def _available(missing: List[_Missing], can_read_bucket: bool) -> bool:
    """Every missing file repairable, and the bucket readable when one of them has to come from it."""
    return all(m.repairable for m in missing) and (can_read_bucket or not any(m.needs_storage for m in missing))


def _file_problem(moves: list, where: dict) -> Optional[str]:
    """The code of the first move whose file is at both paths or at neither, as _recover meets it; None if none is."""
    for move in moves:
        place = where[_key(move)]
        if place in ("both", "neither"):
            return adopt_journal.AT_BOTH if place == "both" else adopt_journal.AT_NEITHER
    return None


def _at_both(moves: list, where: dict) -> list:
    """Each file at both its old and its new path: recovery stops on it whatever the repair places, so none is placed."""
    return [{"kind": m["kind"], _owner(m)[0]: _owner(m)[1], "old_path": m["old"], "new_path": m["new"]}
            for m in moves if where[_key(m)] == "both"]


def _owner(move: dict) -> tuple:
    id_key = "shot_id" if "shot_id" in move else "image_id"
    return id_key, move.get(id_key)


def _key(move: dict) -> tuple:
    return move["old"], move["new"]


def _where(moves: list) -> dict:
    """{(old, new): "old" | "new" | "both" | "neither"} for every move, two lexists each."""
    out = {}
    for move in moves:
        at_old, at_new = os.path.lexists(move["old"]), os.path.lexists(move["new"])
        out[_key(move)] = "both" if at_old and at_new else "old" if at_old else "new" if at_new else "neither"
    return out


def _chunks(ids: list, size: int = 500):
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


def _verified_rows(db: Session, table, owner_col, retired_col, owners: set, kinds: tuple) -> dict:
    """{(owner, kind): (key, sha1)} of verified rows with a SHA1, the owner's own row before one retired from it."""
    from sqlalchemy import or_

    found = {}
    for chunk in _chunks(sorted(o for o in owners if o is not None)):
        rows = (db.query(table.object_key, table.local_sha1, table.kind, owner_col, retired_col)
                .filter(table.kind.in_(kinds), table.verified_at.isnot(None), table.local_sha1.isnot(None),
                        or_(owner_col.in_(chunk), retired_col.in_(chunk)))
                .all())
        for key, sha1, kind, own, retired in rows:
            if own is not None:
                found[(own, kind)] = (key, sha1)
            elif retired is not None:
                found.setdefault((retired, kind), (key, sha1))
    return found


def _missing(db: Session, journal: dict, landed: Optional[bool], where: Optional[dict] = None) -> List[_Missing]:
    """Every move of the journal whose file is at neither path, each repairable or not (module header); read-only.

    Every lookup goes through indexes built once: the moves by (kind,
    owner), the files' places, and the rows read in chunks of 500 ids.
    """
    from app.core.paths import is_raw_suffix
    from app.models.colour_card import ColourCardObject, ColourCardShot
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    moves = journal.get("moves") or []
    where = where if where is not None else _where(moves)
    by_move = {(m["kind"], _owner(m)[1]): m for m in moves}
    absent = [m for m in moves if where[_key(m)] == "neither"]

    def source_kind(owner) -> str:
        master = by_move.get(("master", owner))
        return "preview" if master is not None and is_raw_suffix(Path(master["old"])) else "master"

    pages, cards = set(), set()
    for move in absent:
        _, owner = _owner(move)
        if move["kind"] in _PAGE_KINDS or move["kind"] == "thumbnail":
            pages.add(owner)
        elif move["kind"] in _CARD_KINDS:
            cards.add(owner)
    page_rows = _verified_rows(db, OffsiteObject, OffsiteObject.record_image_id, OffsiteObject.retired_image_id,
                               pages, tuple(_PAGE_KINDS.values())) if pages else {}
    card_rows = _verified_rows(db, ColourCardObject, ColourCardObject.shot_id, ColourCardObject.retired_shot_id,
                               cards, tuple(_CARD_KINDS.values())) if cards else {}
    capture_ids, card_digests = {}, {}
    for chunk in _chunks(sorted(pages)):
        capture_ids.update(db.query(RecordImage.id, RecordImage.capture_id).filter(RecordImage.id.in_(chunk)).all())
    for chunk in _chunks(sorted(cards)):
        card_digests.update((sid, (a, b)) for sid, a, b in db.query(
            ColourCardShot.id, ColourCardShot.sha256, ColourCardShot.preview_sha256).filter(
            ColourCardShot.id.in_(chunk)))
    db.rollback()

    out: List[_Missing] = []
    by_missing = {}
    for move in absent:
        id_key, owner = _owner(move)
        kind = move["kind"]
        m = _Missing(kind, Path(move["old"]), Path(move["new"]), id_key, owner)
        out.append(m)
        by_missing[(kind, owner)] = m
        if kind in _BUFFER_KINDS:
            m.why_not = "never_uploaded"
        elif landed is None:
            m.why_not = "undecided"
        elif kind in _PAGE_KINDS:
            row = page_rows.get((owner, _PAGE_KINDS[kind]))
            if row is None:
                m.why_not = "no_verified_object"
            else:
                m.key, m.sha1 = row
                m.capture_id = capture_ids.get(owner)
        elif kind in _CARD_KINDS:
            row = card_rows.get((owner, _CARD_KINDS[kind]))
            if row is None:
                m.why_not = "no_verified_object"
            else:
                m.key, m.sha1 = row
                digests = card_digests.get(owner)
                if digests:
                    m.sha256 = digests[0] if kind == "colour_card_shot" else digests[1]
        elif kind == "thumbnail":
            _thumbnail_source(m, source_kind(owner), by_move, by_missing, where, page_rows, capture_ids)
        else:
            m.why_not = "unknown_kind"
    return out


def _thumbnail_source(m: _Missing, kind: str, by_move: dict, by_missing: dict, where: dict, page_rows: dict,
                      capture_ids: dict) -> None:
    """Name the verified bytes a thumbnail is made from, or why it cannot be made (module header)."""
    move = by_move.get((kind, m.owner))
    if move is None:
        m.why_not = "thumbnail_source_missing"
        return
    place = where[_key(move)]
    lacking = by_missing.get((kind, m.owner))
    if place == "neither":
        if lacking is None or not lacking.repairable:
            m.why_not = "thumbnail_source_missing"
            return
        m.source = (kind, m.owner)          # the fetched copy, verified when it is fetched
        return
    if place == "both":
        m.why_not = "thumbnail_source_unverified"
        return
    row = page_rows.get((m.owner, _PAGE_KINDS[kind]))
    if row is None:
        m.why_not = "thumbnail_source_unverified"
        return
    m.source = (kind, m.owner)
    m.source_path = Path(move[place])
    m.source_sha1 = row[1]
    m.capture_id = capture_ids.get(m.owner)


# ---------------------------------------------------------------------------
# The repair
# ---------------------------------------------------------------------------

def repair(db: Session, actor: Optional[str] = None, run_id: Optional[str] = None) -> dict:
    """POST /system/adoption/repair (module header); raises RepairRefused for the refusals.

    Both locks are taken without waiting, and whether each was taken goes to
    precheck, which refuses on it as on every other check. Holding
    ADOPTION_LOCK, the repair publishes itself (adopt_journal.running).
    With `run_id`, a journal with another run id, or none, is refused with
    `journal_changed` before anything else is checked or done. Every
    refusal raised once the journal was read names its volumes (`source`,
    `target`, None when it names none or cannot be read), and so does the
    answer.
    """
    got_repair = _REPAIR_LOCK.acquire(blocking=False)
    try:
        got_adoption = got_repair and adopt_journal.ADOPTION_LOCK.acquire(blocking=False)
        try:
            if got_adoption:
                adopt_journal.publish_running("repair")   # never raises
            return _repair(db, actor, repair_running=not got_repair,
                           adoption_running=got_repair and not got_adoption, run_id=run_id)
        finally:
            if got_adoption:
                # Nested, so the release runs whatever the withdrawal does (it never raises).
                try:
                    adopt_journal.clear_running()
                finally:
                    adopt_journal.lock_holder_request_id = None
                    adopt_journal.ADOPTION_LOCK.release()
    finally:
        if got_repair:
            _REPAIR_LOCK.release()


def _named(db: Session, journal) -> tuple:
    """(source, target) of a journal read, each {id, name, filename_stem} or None."""
    if not isinstance(journal, dict):
        return None, None
    try:
        return _volume(db, journal.get("source_id")), _volume(db, journal.get("target_id"))
    except Exception:  # noqa: BLE001 - a malformed id names no volume
        db.rollback()
        return None, None


def _repair(db: Session, actor: Optional[str], *, repair_running: bool, adoption_running: bool,
            run_id: Optional[str] = None) -> dict:
    try:
        journal = adopt_journal.read_journal()
    except adopt_journal.JournalUnreadable:
        journal = None
    else:
        journal = NO_JOURNAL if journal is None else journal
    if not (repair_running or adoption_running):
        adopt_journal.refine_running(journal)
    source, target = _named(db, journal)
    db.rollback()
    try:
        return _repair_read(db, actor, journal, repair_running=repair_running, adoption_running=adoption_running,
                            run_id=run_id, named={"source": source, "target": target})
    except RepairRefused as exc:
        exc.extra.setdefault("source", source)
        exc.extra.setdefault("target", target)
        raise


def _repair_read(db: Session, actor: Optional[str], journal, *, repair_running: bool, adoption_running: bool,
                 run_id: Optional[str], named: dict) -> dict:
    from app.core.export_jobs import export_jobs

    if run_id is not None and not (repair_running or adoption_running) and \
            (not isinstance(journal, dict) or journal.get("run_id") != run_id):
        raise RepairRefused(409, "journal_changed", RUN_CHANGED_MESSAGE, reason="journal_changed")
    checked = precheck(db, journal, repair_running=repair_running, adoption_running=adoption_running)
    db.rollback()
    if checked.refusal is not None:
        checked.raise_refusal()
    run_id = journal.get("run_id")
    staging = checked.staging
    with ExitStack() as stack:
        try:
            stack.enter_context(export_jobs.hold_for_file_changes(checked.volume_ids, HELD_MESSAGE))
        except HTTPException as exc:
            raise RepairRefused(409, "volume_held", HELD_MESSAGE, reason=VOLUME_HELD, detail=str(exc.detail)) from exc
        # The repair's first write: whatever is at the staging name goes (module header).
        if not _sweep_staging(staging):
            raise RepairRefused(409, "repair_write_failed", WRITE_MESSAGE, reason="staging_not_cleared",
                                staging=str(staging))
        placed = _fetch_and_place(checked.landed, checked.missing, staging) if checked.missing else []
        try:
            adopt_journal._require_own(run_id)
        except adopt_journal.JournalTaken as exc:
            raise RepairRefused(409, "journal_changed", CHANGED_MESSAGE,
                                fetched=[_entry(m) for m in placed]) from exc
        result = adopt_journal.recover_holding_lock(db)
    return {**_answer(db, journal, actor, placed, result), **named}


def _entry(m: _Missing) -> dict:
    return {"kind": m.kind, m.id_key: m.owner, "path": str(m.dest), "key": m.key}


def _answer(db: Session, journal: dict, actor: Optional[str], placed: List[_Missing], result: Optional[dict]) -> dict:
    from app.core import audit

    outcome = (result or {}).get("outcome")
    answer = {"result": {"completed": "finished", "reverted": "reverted"}.get(outcome, "unfinished"),
              "reason": (result or {}).get("reason"),
              "fetched": [_entry(m) for m in placed if m.kind != "thumbnail"],
              "derived": [_entry(m) for m in placed if m.kind == "thumbnail"]}
    if answer["result"] == "unfinished":
        answer["code"] = (result or {}).get("code")
        answer["message"] = KEPT_MESSAGE if placed else MESSAGES.get(answer["code"], MESSAGES[NOT_RUN])
    names = ", ".join(m.dest.name for m in placed)
    audit.log_event(db, level="WARN", category=CATEGORY, action=REPAIRED_ACTION, actor=actor,
                    subject=journal.get("target_stem"),
                    detail=(f"source={journal.get('source_id')} ({journal.get('source_stem')}) "
                            f"target={journal.get('target_id')} result={answer['result']} "
                            f"files={len(placed)}: {names}")[:500])
    return answer


def staging_dir() -> Path:
    """The one directory the repair stages files in (module header): flat, in no volume, under the current root.

    Computed once per repair and passed to every step and every cleanup;
    never taken from a journal.
    """
    from app.core import config
    from app.core.paths import ADOPTION_REPAIR_STAGING_DIR

    return config.settings.projects_dir / ADOPTION_REPAIR_STAGING_DIR


def _device(path) -> int:
    return os.stat(path).st_dev


def _same_filesystem(staging: Path, missing: List["_Missing"], landed: Optional[bool]) -> bool:
    """Whether the staging directory (or the root it is made in) and every destination are on one file system.

    The final step is a rename, which cannot cross file systems. A path not
    there yet is judged by its nearest ancestor that is; one stat per
    distinct directory.
    """
    devices = {}

    def device(path: Path) -> int:
        if path not in devices:
            probe = path
            while True:
                try:
                    devices[path] = _device(probe)
                    break
                except FileNotFoundError:
                    if probe.parent == probe:
                        raise
                    probe = probe.parent
        return devices[path]

    if not missing or landed is None:
        return True
    try:
        here = device(staging)
        return all(device((m.new if landed else m.old).parent) == here for m in missing)
    except OSError:
        return False


def _sweep_staging(path: Path) -> bool:
    """Remove whatever is at the staging name `path`; True when nothing is left there. Never raises.

    The name belongs to the repair by itself (module header): a directory
    goes whole, by shutil.rmtree, which removes a symbolic link inside it
    without following it; a symbolic link at the name is unlinked, never
    followed; a file is unlinked.
    """
    import shutil

    try:
        if not os.path.lexists(path):
            return True
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)
        return not os.path.lexists(path)
    except Exception as exc:  # noqa: BLE001 - a sweep never raises
        logger.error(f"Adoption repair: {path} could not be removed: {exc!r}")
        return False


def sweep_staging_at_startup() -> None:
    """Startup recovery's sweep of the staging directory under the current projects root; never raises."""
    try:
        _sweep_staging(staging_dir())
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Adoption repair: the staging sweep at startup failed: {exc!r}")


def _staged_name(m: "_Missing") -> str:
    return f"{secrets.token_hex(6)}-{m.dest.name}.part"


def _place(staging: Path, dest: Path) -> None:
    os.rename(staging, dest)


def _fetch_and_place(landed: bool, missing: List[_Missing], staging: Path) -> List[_Missing]:
    """Fetch and verify every missing file, derive the thumbnails from verified bytes, then place them all.

    Everything is staged in `staging`, the one directory this repair uses
    from start to end, cleanups included; the renames into the volumes are
    the only writes there, made once every file is ready. A refusal or an
    error before them removes the staging directory and changes nothing.
    """
    from app.core import offsite_free
    from app.core.storage_override import refuse_writes_under_unusable_root

    ready: List[tuple] = []
    fetched = {}
    index = None
    try:
        for m in missing:
            m.dest = m.new if landed else m.old
            _check_destination(m.dest)
        refuse_writes_under_unusable_root([m.dest for m in missing] + [staging])
        staging.mkdir(mode=0o700)
        for m in (x for x in missing if x.kind != "thumbnail"):
            path = staging / _staged_name(m)
            rc, error = offsite_free._copy_result(m.key, path)
            if error is not None:
                logger.warning(f"Adoption repair: {error}")
                if rc in offsite_free.ABSENT_RETURN_CODES:
                    raise RepairRefused(409, "repair_object_missing", OBJECT_MISSING_MESSAGE, file=_entry(m))
                raise RepairRefused(503, "repair_copy_failed", COPY_FAILED_MESSAGE, file=_entry(m))
            index = _verify(path, m, m.sha1, m.sha256, index, "repair_digest_mismatch")
            offsite_free._fsync_file(path)
            fetched[(m.kind, m.owner)] = path
            ready.append((path, m))
        for m in (x for x in missing if x.kind == "thumbnail"):
            source = fetched.get(m.source)
            if source is None:
                # A copy on disk: hashed now against what the records hold for it.
                index = _verify(m.source_path, m, m.source_sha1, None, index, "repair_not_possible",
                                source_kind=m.source[0])
                source = m.source_path
            ready.append((_derive_thumbnail(m, source, staging), m))
        for _, m in ready:
            if os.path.lexists(m.old) or os.path.lexists(m.new):
                raise RepairRefused(409, "repair_destination_taken", TAKEN_MESSAGE, file=_entry(m))
        for path, m in ready:
            _place(path, m.dest)
        for directory in sorted({m.dest.parent for _, m in ready}):
            offsite_free._fsync_dir(directory)
    except OSError as exc:
        logger.error(f"Adoption repair: a fetched file could not be written: {exc}")
        _sweep_staging(staging)
        raise RepairRefused(409, "repair_write_failed", WRITE_MESSAGE) from exc
    except Exception:
        _sweep_staging(staging)
        raise
    # A BaseException (a kill) leaves the staging directory as a dead process would; the next repair or start
    # removes it.
    _sweep_staging(staging)
    return [m for _, m in ready]


def _verify(path: Path, m: "_Missing", sha1: Optional[str], sha256: Optional[str], index, code: str,
            source_kind: Optional[str] = None):
    """Check `path` against the SHA1 and the sha256 recorded for it; return the fixity index, built at most once.

    For a fetched file the refusal is `code`; for a thumbnail's source on
    disk it is repair_not_possible, the thumbnail being
    `thumbnail_source_unverified`.
    """
    from app.core import offsite_free
    from app.core.integrity import _sha256

    def refuse(digest):
        if source_kind is not None:
            raise RepairRefused(409, "repair_not_possible", NOT_POSSIBLE_MESSAGE,
                                missing=[{**m.as_dict(), "repairable": False,
                                          "why_not": "thumbnail_source_unverified"}])
        raise RepairRefused(409, code, DIGEST_MESSAGE, file=_entry(m), digest=digest)

    if offsite_free._stable_sha1(path) != sha1:
        refuse("sha1")
    kind = source_kind or m.kind
    if sha256 is None and kind in _PAGE_KINDS:
        index = index or _fixity_index()
        sha256 = _page_sha256(index, kind, m)
    if sha256 is not None and _sha256(path) != sha256:
        refuse("sha256")
    return index


def _check_destination(dest: Path) -> None:
    from app.core.paths import resolve_within_storage

    if resolve_within_storage(str(dest)) is None:
        raise RepairRefused(409, "repair_not_possible", NOT_POSSIBLE_MESSAGE,
                            missing=[{"path": str(dest), "why_not": "outside_storage"}])


def _fixity_index():
    from app.core import config
    from app.core.integrity import build_fixity_index

    return build_fixity_index(config.settings.projects_dir.resolve())


def _page_sha256(index, kind: str, m: "_Missing") -> Optional[str]:
    """The manifests' sha256 for an image's master (by the image) or preview (by its path), or None.

    For a thumbnail's source, `m` is the thumbnail and the paths looked up
    are its source's; for a fetched file, its own.
    """
    old, new = (m.old, m.new) if m.source_path is None else (m.source_path, m.source_path)
    if kind == "master":
        found = index.expected(m.capture_id, m.owner, old.resolve())
        return found or index.expected(m.capture_id, None, new.resolve())
    return index.expected(m.capture_id, None, old.resolve()) or index.expected(m.capture_id, None, new.resolve())


def _derive_thumbnail(m: "_Missing", source: Path, staging: Path) -> Path:
    """Make the thumbnail again from verified bytes, written by generate_thumbnail into the staging directory."""
    from app.core import offsite_free, thumbnail

    try:
        made = thumbnail.generate_thumbnail(source, staging)
    except Exception as exc:  # noqa: BLE001 - Pillow raises many kinds
        logger.warning(f"Adoption repair: the thumbnail {m.dest.name} could not be made from {source}: {exc}")
        made = None
    if not made:
        raise RepairRefused(409, "repair_thumbnail_failed", THUMBNAIL_MESSAGE, file=_entry(m))
    offsite_free._fsync_file(Path(made))
    return Path(made)
