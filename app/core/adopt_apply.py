"""Applying the adoption of a hand-made volume's captures into the volume Fisqua assigned (NEH-387).

adopt_captures does what app.core.adopt_captures.plan_adopt_captures plans,
one adoption at a time on the unit: it runs under
app.core.adopt_journal.ADOPTION_LOCK, and a second adoption that finds the
lock taken is refused at once (`adoption_in_progress`). In this order:

1. Both volumes are held for file changes (export_jobs.hold_for_file_changes)
   until the end, so no export of either runs and the offsite upload reads
   neither (it gets FilesChanging and skips), and no USB copy of either
   starts (export_jobs.hold_for_copy). A volume an export is queued or
   running for, whose files the upload is reading, or which a USB copy is
   reading, is refused (`volume_held`). The hold does not exclude another
   request that changes files; the row locks below do.
2. The plan is made once without locks, and any refusal ends the adoption
   there. Then the bucket is listed under `<tenant>/<target stem>/`, with
   no row locked: any object there refuses (`target_has_bucket_objects`),
   and a listing that cannot answer refuses with 503 (`listing_unavailable`);
   it is never read as empty. The hold keeps this unit's upload away from
   both volumes until the end.
3. The source's records are locked FOR UPDATE in id order, then both volume
   rows FOR UPDATE in id order: records before volumes, as every path that
   takes both does (app/core/record_sequence.py, app/core/volume_stem.py).
   With the volume rows held, the source's record ids are read again; when
   a record came in between, the transaction is rolled back and the locks
   taken again, as record_sequence.lock_volume_records does, and after
   LOCK_ATTEMPTS changed sets the adoption is refused
   (`source_records_changed`).
4. The plan is made again under the locks; any refusal is the adoption's,
   and so is a plan that counts other records than were locked.
5. The source's manifest is read, and the lines for both manifests built
   (app.core.adopt_manifest.adopt_manifest_lines).
6. The source's relocation records are appended to the source's manifest.
   They stay there whatever happens next: each names its image and the
   sha256 the source's capture records already give it, so the fixity
   index reads the same digest for the image whether or not its files
   moved, and whatever its path.
7. Every offsite row of every source image is retired
   (app.core.offsite_retired.retire_rows), in this transaction; one not
   verified refuses (`offsite_rows_not_verified`) with nothing moved.
8. The journal is created, exclusively (app.core.adopt_journal, phase
   `renaming`): every rename the adoption will make, the images, records
   and retired rows, and the target's lines. One that exists already is
   another run's: the adoption is refused (`adoption_unfinished`) and that
   journal left as it is. Then the target's four directories are made
   (capture.project_manager.ensure_volume_dirs) and every file is renamed
   with os.rename, never shutil.move, so a move across devices fails rather
   than copies: each master, its preview, its `.raw` buffer and its
   thumbnail (when it moves), each destination checked to be absent just
   before its rename. The renames done so far are kept in memory for the
   undo; the journal lists all of them before the first. Just before the
   first rename, every path the adoption reads from or renames to is
   checked again to resolve inside the storage, by the plan's own check
   (adopt_captures.outside_storage: `source_outside_storage`,
   `target_outside_storage`). A rename
   counts only once its directories are fsynced: every phase ends with
   adopt_journal.sync_journal_directories, which syncs the directories the
   journal names (directories_to_sync): after the last rename, before the
   rows are written and committed, as volume_layout._commit_batch fsyncs
   its moves' directories before its commit; and after the target's lines
   are appended, before the journal is removed.
9. The rows are written: the records into the target with the sequences
   after the target's highest, in their listing order (as move-records
   assigns them), each image's filename, file_path, thumbnail_path and
   pair_id, the target's capture_counter, and the source's
   captures_adopted_at, which closes it (app.models.collection; an undone
   adoption leaves it null, a finished one, by recovery too, leaves it set,
   since the commit decides both). The source must then hold no
   record (`source_records_changed` otherwise, once everything is put
   back). Then the commit, and the journal's phase `committed`.
10. The target's lines are appended to the target's manifest, the journal's
    phase set to `lines_appended`, and the journal removed. If the append
    fails, the lines are logged at ERROR, the journal stays at `committed`
    with the lines in it, the summary says they are pending
    (`target_lines_pending`), and startup recovery appends them. A journal
    rewrite or removal that fails once the commit has landed does not make
    the adoption fail: the summary says recovery will finish it at the next
    start (`recovery_pending`), as it does when the lines are pending, and
    `volume_captures_adoption_finish_pending` (WARN) goes to the system log.
11. The holds are released, and the adoption is logged as
    `volume_captures_adopted`.

A failure (an Exception) from step 8 on is handled as
storage_ops.relocate_images_and_commit handles one, except that the files
are put back while the row locks are held. A journal, directory, rename or
row write that fails moves every file already renamed back, last first,
never onto a file that is there, removes the directories the adoption was
to make when they are empty, syncs the journal's directories, and only
then rolls the session back (the
retirement too). A commit that raises has ended its transaction and
released its locks; the session is rolled back and the images' rows read
again (storage_ops._relocation_landed): rows holding the new paths mean it
landed, and the adoption goes on with a WARNING; rows holding the old paths
mean it did not, and the source's records and both volume rows are locked
again, the files moved back, and the session rolled back; rows that cannot
tell leave every file where it is and log every (old, new) pair at ERROR.
The journal is removed when everything is back where it was, and kept
otherwise, for startup recovery. Each of these outcomes is written to the
unit's system log: `volume_captures_adoption_reverted` (WARN) when
everything is back, `volume_captures_adoption_not_restored` (ERR) when a
file could not be put back, `volume_captures_adoption_outcome_unknown`
(ERR). storage_ops.undo_relocations is not used: it moves masters only,
with shutil.move.

A BaseException that is not an Exception (an interrupt) rolls the session
back, touches no file, keeps the journal and propagates: what it leaves is
what a killed process leaves, and startup recovery
(app.core.adopt_journal.recover_adoption_at_startup) finishes or undoes it.

Colour card shots move with the pages, in the same transaction and journal
(app.core.adopt_captures.ShotRename). In order: step 7 also retires every
colour_card_objects row of the source's shots (offsite_retired.
retire_card_rows; one not verified refuses, nothing moved); step 8 makes
the target's `calibration/` with its four directories and renames each
shot's master, preview and `.raw` buffer after the pages' files; step 9
moves the source's colour card sets to the target and writes each shot's
filename, file_path, preview_filename and preview_path, and the source must
then hold no set either. The source's relocation records and the target's
lines carry the shots too (app.core.adopt_manifest). A source with no
colour card set writes exactly what it wrote before: no shot key in the
journal, the same moves, rows and lines.

Version: 0.3.3
"""

import json
import logging
import os
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core import adopt_journal, offsite_retired
from app.core.storage_ops import _relocation_landed, _rollback_logged

logger = logging.getLogger(__name__)

ACTION = "volume_captures_adopted"
REVERTED_ACTION = "volume_captures_adoption_reverted"
PENDING_ACTION = "volume_captures_adoption_finish_pending"
NOT_RESTORED_ACTION = "volume_captures_adoption_not_restored"
UNKNOWN_ACTION = "volume_captures_adoption_outcome_unknown"
CATEGORY = "system"
OPERATION = "Adoption of captures"
LOCK_ATTEMPTS = 5

IN_PROGRESS_MESSAGE = "Another adoption of captures is running on this unit. Try again when it has finished."
HELD_MESSAGE = ("An export of one of these volumes is queued or running. Wait for it to finish, then try again.")
LISTING_MESSAGE = ("The bucket could not be listed, so it is not known whether the target's stem is free. "
                   "Nothing was changed. Try again when the bucket can be reached.")
NOT_EMPTY_MESSAGE = ("The bucket already holds objects under the target's stem, so the adoption's keys may "
                     "be taken. Nothing was changed.")
REFUSED_MESSAGE = "The adoption is refused. Nothing was changed."
CHANGED_MESSAGE = ("Records were added to the source while it was being adopted. Nothing was changed; "
                   "try again.")
NOT_VERIFIED_MESSAGE = ("An offsite object of the source is not verified, so its key cannot be retired. "
                        "Nothing was changed.")


class AdoptRefused(Exception):
    """The adoption was refused and nothing it changed is left changed; `codes` and `details` say why."""

    def __init__(self, status_code: int, codes: list, details: Optional[dict] = None, message: str = REFUSED_MESSAGE):
        self.status_code = status_code
        self.codes = list(codes)
        self.details = dict(details or {})
        self.message = message
        super().__init__(f"{message} ({', '.join(self.codes)})")

    def as_detail(self) -> dict:
        return {"message": self.message, "codes": self.codes, "details": self.details}


class AdoptFailed(Exception):
    """The adoption failed after it began to move files; `restored` says whether everything is as it was.

    The message names no path; the paths are in the log.
    """

    status_code = 500

    def __init__(self, message: str, *, restored: bool):
        self.message = message
        self.restored = restored
        super().__init__(message)

    def as_detail(self) -> dict:
        return {"message": self.message, "restored": self.restored}


class _SourceChanged(Exception):
    """The source still holds a record or a colour card set once its records and sets were moved."""


class _DestinationTaken(Exception):
    pass


def _rename(src: Path, dst: Path) -> None:
    os.rename(src, dst)


def _rename_back(new: Path, old: Path) -> None:
    os.rename(new, old)


class _OutsideStorage(Exception):
    """A path the adoption reads from or renames to resolves outside the storage; args[0] holds the codes."""


def bucket_keys_under_stem(tenant: str, stem: str) -> list:
    """Every object key under `<tenant>/<stem>/` in a fresh listing, sorted. Raises VerificationUnavailable.

    The stem's directory is listed first (image_upload._entries), which
    reads its absence only on positive evidence; when it is there, it is
    listed recursively, files only.
    """
    from app.core.catalogue_backup import lsjson_rows
    from app.core.image_upload import _entries, remote_path

    prefix = f"{tenant}/{stem}"
    if _entries(prefix, tenant) is None:
        return []
    rows = lsjson_rows(remote_path(prefix) + "/", recursive=True)
    return sorted(f"{prefix}/{row['Path']}" for row in rows or [] if not row.get("IsDir"))


def _append_target_lines(volume_root: Path, lines: list) -> None:
    from capture.manifestHandler import append_manifest_lines

    append_manifest_lines(volume_root, [json.dumps(line, ensure_ascii=False) for line in lines])


def adopt_captures(db: Session, source_id: int, target_id: int, *, actor: Optional[str],
                   now: Optional[datetime] = None,
                   list_target_prefix: Optional[Callable[[str, str], list]] = None) -> dict:
    """Adopt the source volume's captures into the target (module header); return a summary.

    Raises AdoptRefused when nothing is left changed but the source's
    relocation records, and AdoptFailed when a step from the journal on
    failed. `list_target_prefix(tenant, stem)` lists the bucket under the
    target's stem (bucket_keys_under_stem by default).
    """
    from app.core import audit
    from app.core.export_jobs import export_jobs

    now = now or datetime.now(timezone.utc)
    lister = list_target_prefix or bucket_keys_under_stem
    if not adopt_journal.ADOPTION_LOCK.acquire(blocking=False):
        raise AdoptRefused(409, ["adoption_in_progress"], message=IN_PROGRESS_MESSAGE)
    try:
        return _adopt_locked(db, source_id, target_id, actor=actor, now=now, lister=lister)
    finally:
        adopt_journal.ADOPTION_LOCK.release()


def _adopt_locked(db: Session, source_id: int, target_id: int, *, actor, now: datetime, lister) -> dict:
    from app.core import audit
    from app.core.export_jobs import export_jobs

    with ExitStack() as stack:
        try:
            stack.enter_context(export_jobs.hold_for_file_changes(sorted({source_id, target_id}), HELD_MESSAGE))
        except HTTPException as exc:
            raise AdoptRefused(409, ["volume_held"], message=str(exc.detail)) from exc
        result = _adopt_held(db, source_id, target_id, actor=actor, now=now, lister=lister)
    audit.log_event(db, level="INFO", category=CATEGORY, action=ACTION, actor=actor,
                    subject=result.pop("_target_stem"),
                    detail=(f"source={source_id} ({result.pop('_source_stem')}) target={target_id} "
                            f"records={result['records']} images={result['images']} "
                            f"files_renamed={result['files_renamed']} rows_retired={result['rows_retired']} "
                            f"counter={result['counter_after']} "
                            + (f"colour_card_sets={result['colour_card_sets']} "
                               f"colour_card_shots={result['colour_card_shots']} "
                               if result["colour_card_sets"] else "")
                            + f"target_lines_appended={result['target_lines_appended']}"))
    return result


def _refuse_plan(db: Session, plan) -> None:
    if plan.refusals:
        db.rollback()
        raise AdoptRefused(409, plan.refusals, plan.details)


def _list_target(plan, lister) -> None:
    from app.core.catalogue_backup import VerificationUnavailable
    from app.core.image_upload import configured_tenant_or_none

    try:
        held = lister(configured_tenant_or_none(), plan.target_stem)
    except VerificationUnavailable as exc:
        logger.warning(f"{OPERATION}: the bucket listing under {plan.target_stem} did not answer: {exc}")
        raise AdoptRefused(503, ["listing_unavailable"], message=LISTING_MESSAGE) from exc
    if held:
        raise AdoptRefused(409, ["target_has_bucket_objects"], {"target_has_bucket_objects": sorted(held)},
                           message=NOT_EMPTY_MESSAGE)


def _lock(db: Session, source_id: int, target_id: int) -> list:
    """Lock the source's records, then both volume rows, until the record set read again under them agrees."""
    from app.core import volume_stem
    from app.models.record import Record

    for _ in range(LOCK_ATTEMPTS):
        records = (db.query(Record).filter(Record.collection_id == source_id)
                   .order_by(Record.id).with_for_update().all())
        for cid in sorted({source_id, target_id}):
            volume_stem.locked_volume_query(db, cid).first()
        now_ids = {rid for (rid,) in db.query(Record.id).filter(Record.collection_id == source_id)}
        if now_ids == {r.id for r in records}:
            return records
        db.rollback()
    raise AdoptRefused(409, ["source_records_changed"], message=CHANGED_MESSAGE)


def _adopt_held(db: Session, source_id: int, target_id: int, *, actor, now: datetime, lister) -> dict:
    from app.core.adopt_captures import plan_adopt_captures
    from app.core.storage_ops import collection_root_dir
    from app.models.collection import Collection

    # 2. The plan without locks, then the bucket, with no row locked.
    plan = plan_adopt_captures(db, source_id, target_id)
    db.rollback()
    _refuse_plan(db, plan)
    _list_target(plan, lister)

    # 3 and 4. Records, then volumes, then the plan under the locks.
    records = _lock(db, source_id, target_id)
    plan = plan_adopt_captures(db, source_id, target_id)
    _refuse_plan(db, plan)
    if plan.records != len(records):
        db.rollback()
        raise AdoptRefused(409, ["source_records_changed"], message=CHANGED_MESSAGE)
    source, target = db.get(Collection, source_id), db.get(Collection, target_id)
    src_root, tgt_root = collection_root_dir(db, source), collection_root_dir(db, target)

    # 5. The lines of both manifests.
    target_lines, source_lines = _manifest_lines(db, plan, src_root, now)

    # 6. The source's relocation records, which stay whatever follows (module header).
    try:
        _append_source_lines(src_root, source_lines)
    except OSError as exc:
        db.rollback()
        logger.error(f"{OPERATION}: the relocation records could not be appended to the manifest of {src_root}: "
                     f"{exc}; nothing was changed")
        raise AdoptFailed("The source's manifest could not be written. Nothing was changed.", restored=True) from exc

    # 7. The offsite rows of every source image, retired in this transaction.
    try:
        retired = offsite_retired.retire_rows(db, [r.image_id for r in plan.renames], now=now)
    except offsite_retired.RetireRefused as exc:
        db.rollback()
        raise AdoptRefused(409, ["offsite_rows_not_verified"], {"offsite_rows_not_verified": exc.unverified},
                           message=NOT_VERIFIED_MESSAGE) from exc
    retired_cards = []
    if plan.shot_renames:
        try:
            retired_cards = offsite_retired.retire_card_rows(db, [r.shot_id for r in plan.shot_renames], now=now)
        except offsite_retired.CardRetireRefused as exc:
            db.rollback()
            raise AdoptRefused(409, ["offsite_rows_not_verified"], {"offsite_rows_not_verified": exc.unverified},
                               message=NOT_VERIFIED_MESSAGE) from exc

    # 8 and 9. The journal, the directories, the files, the rows, then the commit.
    #
    # Shots, in the order pages have them: (a) the shots' offsite rows are retired above, in this
    # transaction, uncommitted; (b) the journal lists every shot rename, the shots' and sets' ids and
    # old and new paths, before the first rename; (c) the shots' files are renamed after the pages';
    # (d) the sets and shots rows are written beside the records and images; (e) one commit lands all
    # rows, the retirement included; (f) the journal is marked committed and the target's lines
    # (with the shots' calibration lines) appended; (g) the upload queues each shot again on its own,
    # since a shot with no row is pending (image_upload.colour_card_work), once the commit is visible.
    # A failure before (e) puts every renamed file back and rolls back (a) and (d) together; a kill
    # leaves the journal, and recovery decides by the records, checks the images' and the shots'
    # rows against that decision, and renames every listed file forward or back. After (e) a
    # failure leaves the adoption landed, and recovery only finishes (f). Nothing is queued before
    # the commit, so a rolled-back adoption leaves no upload of a new key.
    moves = planned_moves(plan)
    to_create = _dirs_to_create(tgt_root, calibration=bool(plan.shot_renames))
    journal = _journal(plan, records, retired, moves, to_create, tgt_root, target_lines, actor=actor, now=now,
                       retired_cards=retired_cards)
    try:
        adopt_journal.create_journal(journal)
    except adopt_journal.JournalTaken as exc:
        db.rollback()
        raise AdoptRefused(409, ["adoption_unfinished"], message=adopt_journal.unfinished_message()) from exc
    except Exception as exc:
        db.rollback()
        logger.error(f"{OPERATION}: the journal could not be written: {exc}; nothing was changed")
        raise AdoptFailed("The adoption's journal could not be written. Nothing was changed.", restored=True) from exc
    except BaseException:
        _rollback_logged(db, OPERATION)
        raise
    moved: List[Tuple[Path, Path]] = []
    try:
        _make_target_dirs(plan.target_stem, tgt_root if plan.shot_renames else None)
        # Again just before the first rename: a directory made a link elsewhere since the plan.
        outside = not_inside_storage(plan)
        if outside:
            raise _OutsideStorage(outside)
        _rename_all(moves, moved)
        adopt_journal.sync_journal_directories(journal)
        _write_rows(db, plan, records, source_id, target_id, now=now)
    except Exception as exc:
        # The row locks are still held: the files go back before the rollback lets them go.
        not_restored = _undo(moved)
        _remove_created(to_create)
        adopt_journal.sync_journal_directories(journal)
        _rollback_logged(db, OPERATION)
        logger.error(f"{OPERATION} of volume {source_id} into {target_id} stopped: {exc}")
        _after_undo(db, journal, not_restored, exc)
    except BaseException:
        _rollback_logged(db, OPERATION)
        raise
    _commit(db, plan, source_id, target_id, moved, to_create, journal)
    # From here the adoption has landed: a journal that cannot be rewritten or removed is finished at the next start.
    pending = False
    journal["phase"] = "committed"
    try:
        adopt_journal.update_journal(journal)
    except (OSError, adopt_journal.JournalTaken) as exc:
        pending = True
        logger.error(f"{OPERATION}: the journal could not be marked committed: {exc}; startup recovery finishes "
                     "the adoption")

    # 10. The target's lines, after the commit.
    appended = True
    try:
        _append_target_lines(tgt_root, target_lines)
    except OSError as exc:
        appended = False
        logger.error(f"{OPERATION}: the adopted lines could not be appended to "
                     f"{tgt_root / 'metadata' / 'manifest.jsonl'}: {exc}. They stay in "
                     f"{adopt_journal.journal_file()}, and startup recovery appends them. "
                     f"The lines: {json.dumps(target_lines, ensure_ascii=False)}")
    if appended and not pending:
        journal["phase"] = "lines_appended"
        try:
            adopt_journal.sync_journal_directories(journal)
            adopt_journal.update_journal(journal)
            adopt_journal.remove_journal(journal["run_id"])
        except (OSError, adopt_journal.JournalTaken) as exc:
            pending = True
            logger.error(f"{OPERATION}: the journal could not be removed: {exc}; startup recovery removes it")
    pending = pending or not appended
    if pending:
        _audit(db, PENDING_ACTION, "WARN", journal,
               f"adopted; {'the target lines are pending; ' if not appended else ''}startup recovery finishes "
               f"it at the next start from {adopt_journal.journal_file()}")
    return {
        "source_id": source_id, "target_id": target_id, "records": len(records), "images": len(plan.renames),
        "files_renamed": len(moved), "rows_retired": len(retired) + len(retired_cards),
        "counter_after": plan.counter_after,
        "colour_card_sets": len(plan.card_set_ids), "colour_card_shots": len(plan.shot_renames),
        "source_lines_appended": len(source_lines), "target_lines_appended": appended,
        "target_lines_pending": not appended, "recovery_pending": pending,
        "_source_stem": plan.source_stem, "_target_stem": plan.target_stem,
    }


def _journal(plan, records, retired, moves, to_create, tgt_root, target_lines, *, actor, now,
             retired_cards=()) -> dict:
    """The journal (app.core.adopt_journal); the shots' keys only when the source has colour card sets."""
    kinds = {}
    for r in plan.renames:
        kinds[(r.old_path, r.new_path)] = ("master", "image_id", r.image_id)
        kinds[(r.old_preview_path, r.new_preview_path)] = ("preview", "image_id", r.image_id)
        kinds[(r.old_raw_buffer_path, r.new_raw_buffer_path)] = ("buffer", "image_id", r.image_id)
        kinds[(r.old_thumbnail_path, r.new_thumbnail_path)] = ("thumbnail", "image_id", r.image_id)
    for r in plan.shot_renames:
        kinds[(r.old_path, r.new_path)] = ("colour_card_shot", "shot_id", r.shot_id)
        kinds[(r.old_preview_path, r.new_preview_path)] = ("colour_card_preview", "shot_id", r.shot_id)
        kinds[(r.old_raw_buffer_path, r.new_raw_buffer_path)] = ("colour_card_buffer", "shot_id", r.shot_id)
    import uuid

    journal = {
        "version": adopt_journal.JOURNAL_VERSION, "run_id": uuid.uuid4().hex, "phase": "renaming",
        "source_id": plan.source_id, "source_stem": plan.source_stem,
        "target_id": plan.target_id, "target_stem": plan.target_stem,
        "actor": actor, "started_at": now.isoformat(),
        "moves": [{"kind": kinds[(old, new)][0], kinds[(old, new)][1]: kinds[(old, new)][2],
                   "old": str(old), "new": str(new)} for old, new in moves],
        "images": [{"id": r.image_id, "record_id": r.record_id, "old_file_path": str(r.old_path),
                    "new_file_path": str(r.new_path)} for r in plan.renames],
        "record_ids": [rec.id for rec in records],
        "retired_row_ids": sorted(row.id for row in retired),
        "created_dirs": [str(d) for d in to_create],
        "source_root": str(plan.renames[0].old_path.parent.parent) if plan.renames else None,
        "target_root": str(tgt_root),
        "target_lines": target_lines,
    }
    if plan.card_set_ids:
        journal["colour_card_set_ids"] = list(plan.card_set_ids)
        journal["colour_card_shots"] = [
            {"id": r.shot_id, "set_id": r.set_id, "old_file_path": str(r.old_path), "new_file_path": str(r.new_path),
             "old_preview_path": str(r.old_preview_path) if r.old_preview_path else None,
             "new_preview_path": str(r.new_preview_path) if r.new_preview_path else None}
            for r in plan.shot_renames]
        journal["retired_colour_card_row_ids"] = sorted(row.id for row in retired_cards)
    return journal


def _manifest_lines(db: Session, plan, src_root: Path, now: datetime) -> tuple:
    """(target lines, source lines) from the source's manifest as it is now; a change since the plan refuses."""
    from app.core.adopt_manifest import adopt_manifest_lines
    from app.core.integrity import read_manifest_lines

    manifest = src_root / "metadata" / "manifest.jsonl"
    parsed = []
    try:
        lines, _ = read_manifest_lines(manifest)
    except OSError as exc:
        db.rollback()
        raise AdoptRefused(409, ["source_capture_line_missing"], message=REFUSED_MESSAGE) from exc
    for line in lines:
        try:
            parsed.append(json.loads(line) if line.strip() else None)
        except json.JSONDecodeError:
            continue
    try:
        return adopt_manifest_lines(plan, parsed, adopted_at=now.isoformat())
    except ValueError as exc:
        db.rollback()
        raise AdoptRefused(409, ["source_manifest_path_unmapped"], message=REFUSED_MESSAGE) from exc


def _append_source_lines(src_root: Path, lines: list) -> None:
    from capture.manifestHandler import append_manifest_lines

    append_manifest_lines(src_root, [json.dumps(line, ensure_ascii=False) for line in lines])


def planned_moves(plan) -> List[Tuple[Path, Path]]:
    """Every (old, new) rename of the plan: each image's master, preview, buffer and thumbnail, then each shot's."""
    moves = []
    for r in list(plan.renames) + list(getattr(plan, "shot_renames", [])):
        for old, new in ((r.old_path, r.new_path), (r.old_preview_path, r.new_preview_path),
                         (r.old_raw_buffer_path, r.new_raw_buffer_path),
                         (r.old_thumbnail_path, r.new_thumbnail_path)):
            if old is not None and new is not None and old != new:
                moves.append((old, new))
    return moves


def not_inside_storage(plan) -> list:
    """The refusal codes of adopt_captures.outside_storage for the plan's paths, read now; empty when none."""
    from app.core.adopt_captures import outside_storage

    source, target = outside_storage(plan.source_stem, plan.target_stem, plan.renames + plan.shot_renames)
    return (["source_outside_storage"] if source else []) + (["target_outside_storage"] if target else [])


def _dirs_to_create(tgt_root: Path, calibration: bool = False) -> List[Path]:
    """The target's root and four directories, and its `calibration/` when shots move, not there yet; deepest first."""
    from app.core.paths import CALIBRATION_DIR, VOLUME_SUBDIRS

    candidates = [tgt_root] + [tgt_root / d for d in VOLUME_SUBDIRS]
    if calibration:
        candidates.append(tgt_root / CALIBRATION_DIR)
    return list(reversed([p for p in candidates if not p.exists()]))


def _make_target_dirs(stem: str, calibration_root: Optional[Path] = None) -> None:
    """The target's four directories, and `calibration/` under `calibration_root` when it is given."""
    from capture import project_manager

    from app.core.paths import CALIBRATION_DIR

    project_manager.ensure_volume_dirs(stem)
    if calibration_root is not None:
        (calibration_root / CALIBRATION_DIR).mkdir(exist_ok=True)


def _remove_created(created: List[Path]) -> None:
    for directory in created:
        try:
            directory.rmdir()
        except OSError:
            pass  # not empty, or never made: left as it is


def _rename_all(moves: List[Tuple[Path, Path]], moved: List[Tuple[Path, Path]]) -> None:
    """Rename each (old, new) pair, appending it to `moved` once it has landed; a destination already there raises."""
    for old, new in moves:
        if os.path.lexists(new):
            raise _DestinationTaken(f"{new} already exists, so {old} was not renamed")
        _rename(old, new)
        moved.append((old, new))


def _write_rows(db: Session, plan, records: list, source_id: int, target_id: int, *, now: datetime) -> None:
    from app.core import record_sequence
    from app.models.collection import Collection
    from app.models.record import Record, RecordImage

    sequences = record_sequence.next_sequences(db, target_id, len(records))
    for rec, seq in zip(record_sequence.listing_order(records), sequences or []):
        rec.collection_id = target_id
        rec.sequence = seq
    by_id = {r.image_id: r for r in plan.renames}
    ids = list(by_id)
    for start in range(0, len(ids), 500):
        for img in db.query(RecordImage).filter(RecordImage.id.in_(ids[start:start + 500])):
            r = by_id[img.id]
            img.filename = r.new_name
            img.file_path = str(r.new_path)
            img.pair_id = r.new_pair_id
            if r.new_thumbnail_path is not None:
                img.thumbnail_path = str(r.new_thumbnail_path)
    db.get(Collection, target_id).capture_counter = plan.counter_after
    # The source is closed in this transaction (Collection.captures_adopted_at): an undone adoption leaves it open.
    db.get(Collection, source_id).captures_adopted_at = now.replace(tzinfo=None)
    if plan.card_set_ids:
        _write_card_rows(db, plan, target_id)
    db.flush()
    if db.query(Record.id).filter(Record.collection_id == source_id).first() is not None:
        raise _SourceChanged("the source still holds a record once its records were moved")
    if plan.card_set_ids:
        from app.models.colour_card import ColourCardSet

        if db.query(ColourCardSet.id).filter(ColourCardSet.collection_id == source_id).first() is not None:
            raise _SourceChanged("the source still holds a colour card set once its sets were moved")


def _write_card_rows(db: Session, plan, target_id: int) -> None:
    """Move the source's colour card sets to the target, numbers kept, and give each shot its new names."""
    from app.models.colour_card import ColourCardSet, ColourCardShot

    for card_set in db.query(ColourCardSet).filter(ColourCardSet.id.in_(plan.card_set_ids)):
        card_set.collection_id = target_id
    by_id = {r.shot_id: r for r in plan.shot_renames}
    ids = list(by_id)
    for start in range(0, len(ids), 500):
        for shot in db.query(ColourCardShot).filter(ColourCardShot.id.in_(ids[start:start + 500])):
            r = by_id[shot.id]
            shot.filename, shot.file_path = r.new_name, str(r.new_path)
            if r.new_preview_path is not None:
                shot.preview_filename, shot.preview_path = r.new_preview_path.name, str(r.new_preview_path)


def _commit(db: Session, plan, source_id: int, target_id: int, moved: List[Tuple[Path, Path]],
            created: List[Path], journal: dict) -> None:
    """Commit the rows; on a failure, find whether it landed and go forward, go back, or log every pair."""
    new_paths = {r.image_id: str(r.new_path) for r in plan.renames}

    def log_pairs() -> None:
        for old, new in moved:
            logger.error(f"{OPERATION}: commit outcome unknown; file left at {new}, originally at {old}")

    try:
        db.commit()
    except Exception as exc:
        # A commit that raises has ended its transaction and released its locks, so the files cannot be put
        # back under them. Between that and the locks taken again below, a capture into the target could
        # take a volume lock and read the names on disk; capture into the target is refused while the
        # journal exists (app.api.cameras), and the journal stays until the files are back.
        rolled_back = _rollback_logged(db, OPERATION)
        landed = _relocation_landed(db, new_paths) if rolled_back else None
        if landed:
            logger.warning(f"{OPERATION}: the commit raised but the images' rows hold their new paths, so it "
                           f"landed; the files stay where they were renamed to: {exc}")
            return
        if landed is None:
            log_pairs()
            logger.error(f"{OPERATION}: the commit failed and its outcome could not be determined; "
                         f"no file was moved back: {exc}")
            _audit(db, UNKNOWN_ACTION, "ERR", journal,
                   f"journal kept at {adopt_journal.journal_file()}; the commit's outcome is unknown, "
                   f"{len(moved)} file(s) left at their new names; startup recovery decides by the rows")
            raise AdoptFailed("The database update failed and its outcome could not be determined; the files "
                              "were left at their new names.", restored=False) from exc
        try:
            _lock(db, source_id, target_id)
        except AdoptRefused:
            pass  # records came in meanwhile: the files go back all the same
        not_restored = _undo(moved)
        _remove_created(created)
        adopt_journal.sync_journal_directories(journal)
        _rollback_logged(db, OPERATION)
        logger.error(f"{OPERATION}: the commit failed; the files were moved back: {exc}")
        _after_undo(db, journal, not_restored, exc)
    except BaseException:
        _rollback_logged(db, OPERATION)
        log_pairs()
        raise


def _after_undo(db: Session, journal: dict, not_restored: List[Path], exc: Exception) -> None:
    """Remove the journal when everything is back, write the outcome to the system log, and raise it."""
    if not not_restored:
        adopt_journal.remove_journal(journal["run_id"])
        _audit(db, REVERTED_ACTION, "WARN", journal, f"stopped and put back: {_reason(exc)}")
        if isinstance(exc, _SourceChanged):
            raise AdoptRefused(409, ["source_records_changed"], message=CHANGED_MESSAGE) from exc
        if isinstance(exc, _OutsideStorage):
            raise AdoptRefused(409, exc.args[0], message=REFUSED_MESSAGE) from exc
        raise AdoptFailed(_stopped_message(exc, not_restored), restored=True) from exc
    logger.error(f"{OPERATION}: {len(not_restored)} file(s) could not be put back: "
                 f"{[str(p) for p in not_restored]}")
    _audit(db, NOT_RESTORED_ACTION, "ERR", journal,
           f"{len(not_restored)} file(s) could not be put back; journal kept at {adopt_journal.journal_file()}; "
           "restarting the backend puts them back, or repair by hand from the log")
    raise AdoptFailed(_stopped_message(exc, not_restored), restored=False) from exc


def _audit(db: Session, action: str, level: str, journal: dict, what: str) -> None:
    from app.core import audit

    detail = f"source={journal['source_id']} ({journal['source_stem']}) target={journal['target_id']}: {what}"
    audit.log_event(db, level=level, category=CATEGORY, action=action, actor=journal.get("actor"),
                    subject=journal["target_stem"], detail=detail[:500])


def _undo(moved: List[Tuple[Path, Path]]) -> List[Path]:
    """Rename each moved file back, last first, never onto a file that is there; return the paths not restored."""
    not_restored = []
    for old, new in reversed(moved):
        if os.path.lexists(old):
            logger.error(f"{OPERATION}: could not put {new} back at {old}: the name is taken; left at {new}")
            not_restored.append(old)
            continue
        try:
            _rename_back(new, old)
        except OSError as exc:
            logger.error(f"{OPERATION}: could not put {new} back at {old}; left at {new}: {exc}")
            not_restored.append(old)
    return not_restored


def _reason(exc: BaseException) -> str:
    if isinstance(exc, _DestinationTaken):
        return "a file the adoption was to create already exists"
    if isinstance(exc, _SourceChanged):
        return "records were added to the source meanwhile"
    return "a directory or file could not be made or renamed, or a row written"


def _stopped_message(exc: BaseException, not_restored: List[Path]) -> str:
    if not_restored:
        return (f"The adoption stopped: {_reason(exc)}. {len(not_restored)} file(s) could not be put back where "
                "they were; the log names them, and restarting the backend puts them back.")
    return f"The adoption stopped: {_reason(exc)}. Every file is back where it was, and no row changed."
