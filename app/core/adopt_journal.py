"""The journal of an adoption in progress, and its recovery at startup (NEH-387).

One adoption at a time on the unit, and the journal is its claim. An
adoption, and the startup recovery, run under ADOPTION_LOCK, a lock of this
process; a second adoption that finds it taken is refused at once. The
journal is created exclusively (O_CREAT | O_EXCL), so two processes cannot
both hold it, and carries the `run_id` of the run that created it: a later
rewrite or removal by a run that did not create it raises JournalTaken and
changes nothing.

app.core.adopt_apply.adopt_captures creates `adopt-captures.journal` in the
data directory before its first rename, and removes it once the commit has
landed, the renamed files' directories are fsynced and the target's
manifest lines are appended. It is JSON; each later phase rewrites it whole
to a temporary name, fsynced and renamed into place
(capture.utils.atomic_write, as app.core.volume_layout writes its journal):

    {"version": 1, "run_id", "phase": "renaming" | "committed" | "lines_appended",
     "source_id", "source_stem", "target_id", "target_stem", "actor", "started_at",
     "moves": [{"kind": "master" | "preview" | "buffer" | "thumbnail",
                "image_id", "old", "new"}, ...],
     "images": [{"id", "record_id", "old_file_path", "new_file_path"}, ...],
     "record_ids", "retired_row_ids",
     "created_dirs": the target directories the adoption makes, deepest first,
     "source_root", "target_root", "target_lines": the lines for the target's manifest}

and, only when the source has colour card sets, `colour_card_set_ids`,
`colour_card_shots` ([{"id", "set_id", "old_file_path", "new_file_path",
"old_preview_path", "new_preview_path"}, ...]) and
`retired_colour_card_row_ids`, with the shots' renames in `moves` after
the images' (kinds `colour_card_shot`, `colour_card_preview`,
`colour_card_buffer`, each with `shot_id` in place of `image_id`).

`moves` is every rename the adoption makes, in order; it is written before
the first, so it says what the adoption will do, not how far it got. The
phase records the last step known to have finished; the database, not the
phase, decides the recovery.

While the journal exists, every adoption is refused (`adoption_unfinished`,
app.core.adopt_captures), and a volume it names is not captured into,
freed, restored, rebuilt, exported, deleted, or given, moved or relieved of
records or images: journal_names_volume answers for each of those paths. A
journal that cannot be read names every volume, since it cannot say which.

recover_adoption_at_startup runs at startup, under ADOPTION_LOCK, before
the volume layout migration and its repair of volumes' manifests
(app/main.py). The records decide: the journal's records, read now, all in
the target mean the commit landed, and all in the source mean it did not.
The images' rows and the files are then checked against that decision,
never used to make it:

* Landed: every image row must hold its new path, and so must every shot
  row (its file and preview paths), with every colour card set in the
  target. Each file still at its
  old path is renamed to its new one, the journal's directories are synced
  (sync_journal_directories), the target's lines its manifest does not yet
  hold are appended, the directories are synced again, and the journal is
  removed.
* Not landed: every image row and shot row must hold its old paths, with
  every colour card set in the source. Each file at its new
  path is renamed back to its old one, the directories the adoption made
  are removed when empty, the journal's directories are synced, and the
  journal is removed. The source's relocation records stay in its
  manifest: each names its image and the sha256 it already had.
* Anything else (records in both volumes, an image row that contradicts
  the records, a file at both paths or at neither): nothing is changed,
  the journal is kept, and an ERROR and an audit event name it, for repair
  by hand. So is a recovery that raises: the backend still starts, and
  `adoption_unfinished` stays in force. Each of these carries a code of
  UNFINISHED_CODES, and the outcome of the last recovery is kept in this
  process (last_recovery) for app.core.adopt_repair, which reports it and
  runs this same recovery again once it has fetched the files it lacks.

Every check is made before the first rename, and a rename is made only
from where the file is to a name that is free, so a recovery killed part
way is completed by the next one.

Version: 0.3.3
"""

import errno
import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.core import config

logger = logging.getLogger(__name__)

JOURNAL_NAME = "adopt-captures.journal"
JOURNAL_VERSION = 1
CATEGORY = "system"
RECOVERY_COMPLETED_ACTION = "volume_captures_adoption_completed_at_startup"
RECOVERY_REVERTED_ACTION = "volume_captures_adoption_reverted_at_startup"
RECOVERY_UNFINISHED_ACTION = "volume_captures_adoption_unfinished"

UNFINISHED_MESSAGE = ("An earlier adoption of captures did not finish, and its journal ({path}) is still there. "
                      "Restart the backend to finish or undo it; if that does not clear it, the unit's system "
                      "log says what needs repair by hand.")


# One adoption, or recovery, at a time in this process (module header).
ADOPTION_LOCK = threading.Lock()

# The code of each way a recovery stops with the journal kept (_recover_locked, _recover).
UNREADABLE = "journal_unreadable"              # the journal cannot be read
FAILED = "recovery_failed"                     # the recovery raised
RECORDS_SPLIT = "records_split"                # the records are not all in the target nor all in the source
IMAGE_ROWS = "image_rows_contradict"           # an image row holds the other side's path
CARD_ROWS = "colour_card_rows_contradict"      # a shot row or a set's volume is the other side's
AT_BOTH = "file_at_both_paths"                 # a file is at its old path and at its new one
AT_NEITHER = "file_at_neither_path"            # a file is at neither
UNFINISHED_CODES = (UNREADABLE, FAILED, RECORDS_SPLIT, IMAGE_ROWS, CARD_ROWS, AT_BOTH, AT_NEITHER)

# The outcome of the last recovery in this process, with the run_id of the journal it read (None when it
# could read none); written under ADOPTION_LOCK.
_last_recovery: Optional[dict] = None


def last_recovery() -> Optional[dict]:
    """{"run_id", "outcome", "code", "reason"} of the last recovery in this process, or None."""
    return dict(_last_recovery) if _last_recovery else None


class JournalUnreadable(RuntimeError):
    """The journal exists but cannot be read; it names no volume that can be trusted."""


class JournalTaken(RuntimeError):
    """A journal exists that this run did not create; it was left as it is."""


def journal_file() -> Path:
    return config.settings.data_dir / JOURNAL_NAME


def journal_exists() -> bool:
    return journal_file().exists()


def _fsync_dir(directory) -> None:
    from capture.utils import _fsync_dir as fsync_dir

    fsync_dir(directory)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
    os.fsync(fd)


def _write_new(path: Path, data: bytes) -> None:
    """Create `path` exclusively and write `data` to it, fsynced; on any failure the file is removed."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        _write_all(fd, data)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)


# The errors os.link raises where the file system has no hard links (exFAT, vfat).
_NO_LINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}


def create_journal(payload: dict) -> None:
    """Create the journal complete or not at all, failing with JournalTaken if one exists; durable on return.

    When it raises, no journal created by this run exists; when it returns,
    the journal exists complete and durable. The whole content is written
    to a temporary file beside it and fsynced, then linked to the journal's
    name with os.link, which fails if a journal exists: that is the
    exclusive claim, and the journal appears with all its bytes. The
    temporary name is then unlinked and the directory fsynced. Once the
    link has succeeded the journal is this run's (it carries the run's
    run_id): any later failure here removes it, after checking it still
    carries that run_id so that a competing run's journal is never removed,
    syncs the directory as far as it can, and re-raises.

    The unit's data directory is on the SD card's ext4, which has hard
    links. On a file system without them (exFAT, vfat: os.link fails with
    EPERM or ENOTSUP) the journal is created with O_CREAT | O_EXCL, written
    and fsynced, and removed on any failure; a power cut during that write
    can leave a partial journal, which reads as unreadable and is kept and
    reported at startup for a person to remove.
    """
    path = journal_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{payload.get('run_id') or os.getpid()}.{threading.get_ident()}.tmp")
    _write_new(tmp, data)
    linked = False
    try:
        try:
            os.link(tmp, path)
            linked = True
        except FileExistsError as exc:
            raise JournalTaken(f"{path} exists, so another adoption holds the unit") from exc
        except OSError as exc:
            if exc.errno not in _NO_LINKS:
                raise
            try:
                _write_new(path, data)
            except FileExistsError as taken:
                raise JournalTaken(f"{path} exists, so another adoption holds the unit") from taken
            linked = True
        _unlink(tmp)
        _fsync_dir(path.parent)
    except BaseException:
        _quietly(lambda: _unlink(tmp))
        if linked:
            _remove_if_own(payload.get("run_id"))
        _quietly(lambda: _fsync_dir(path.parent))
        raise


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _quietly(step) -> None:
    try:
        step()
    except OSError as exc:
        logger.warning(f"Adoption journal: a clean-up step failed and was skipped: {exc}")


def _remove_if_own(run_id) -> None:
    """Remove the journal when it carries `run_id`; a journal of any other run, or one unreadable, is left."""
    try:
        current = read_journal()
    except JournalUnreadable:
        return
    if current is not None and current.get("run_id") == run_id:
        _quietly(lambda: _unlink(journal_file()))


def directories_to_sync(journal: dict) -> list:
    """Every directory whose entries an adoption can create or change, from its journal alone, deepest first.

    The directory entries an adoption creates or changes, and why each is here:

    * a renamed file leaves its old directory and enters its new one, each
      a directory entry change: the parent of every `old` and every `new`
      in `moves` (masters, previews, buffers, thumbnails; undo and recovery
      rename the same pairs the other way);
    * a directory the adoption makes (the target's root and its four
      directories) is an entry in its parent, and a directory it removes
      when empty is one too: every directory in `created_dirs`, and the
      parent of each;
    * the target's root is created in the projects root, and the target's
      manifest may be created in it: the target's root and its parent;
    * the source's relocation records are appended to the source's
      manifest, and the source's directories lose their files: the source's
      root;
    * each manifest is a directory entry in its volume's `metadata/` (the
      source's appended to, the target's created by the first append): the
      metadata directory of both volumes.

    Nothing else is created, renamed or removed: the journal itself is in
    the data directory, which create_journal, update_journal and
    remove_journal sync themselves. Deepest first, so a hierarchy made by
    the adoption is durable from its leaves up to an ancestor that already
    was.
    """
    dirs = set()
    for move in journal.get("moves") or []:
        for key in ("old", "new"):
            if move.get(key):
                dirs.add(str(Path(move[key]).parent))
    for created in journal.get("created_dirs") or []:
        dirs.add(str(Path(created)))
        dirs.add(str(Path(created).parent))
    for key in ("target_root", "source_root"):
        if journal.get(key):
            root = Path(journal[key])
            dirs.add(str(root))
            dirs.add(str(root / "metadata"))
    if journal.get("target_root"):
        dirs.add(str(Path(journal["target_root"]).parent))
    return sorted(dirs, key=lambda d: (-len(Path(d).parts), d))


def sync_journal_directories(journal: dict) -> None:
    """Fsync each of directories_to_sync(journal) that exists, in its order; every phase ends with this."""
    for directory in directories_to_sync(journal):
        if Path(directory).is_dir():
            _fsync_dir(directory)


def _require_own(run_id) -> None:
    try:
        current = read_journal()
    except JournalUnreadable as exc:
        raise JournalTaken(str(exc)) from exc
    if current is None or current.get("run_id") != run_id:
        raise JournalTaken(f"{journal_file()} is not the journal of run {run_id}")


def update_journal(payload: dict) -> None:
    """Rewrite this run's journal whole, durably; JournalTaken when the journal there is not this run's.

    After an exception the journal is still this run's and complete, holding
    either its earlier content or the new one: the rewrite goes to a
    temporary name and replaces the journal in one rename
    (capture.utils.atomic_write), and a failure after that rename (the
    directory's sync) leaves the new content in place, not yet durable.
    """
    from capture.utils import atomic_write

    _require_own(payload.get("run_id"))
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    atomic_write(journal_file(), lambda tmp: Path(tmp).write_text(text, encoding="utf-8"))


def read_journal() -> Optional[dict]:
    """The journal, or None; raises JournalUnreadable for one that exists and cannot be read."""
    path = journal_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JournalUnreadable(f"{path} cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise JournalUnreadable(f"{path} does not hold a journal")
    return data


def remove_journal(run_id: Optional[str] = None) -> None:
    """Remove the journal; with `run_id`, only that run's (JournalTaken otherwise).

    On return the journal is gone. On an exception it is still there: the
    unlink is the step that can fail with the journal kept, and the
    directory's sync after it is made as far as it can be and logged when
    it fails, never raised, so a caller is never told a removed journal
    remains. A removal whose sync failed and that a power cut then undoes
    leaves a journal whose adoption is already finished, which recovery
    completes without changing anything.
    """
    if run_id is not None:
        _require_own(run_id)
    path = journal_file()
    _unlink(path)
    _quietly(lambda: _fsync_dir(path.parent))


def unfinished_message() -> str:
    return UNFINISHED_MESSAGE.format(path=journal_file())


def journal_names_volume(collection_id: Optional[int]) -> bool:
    """Whether an adoption's journal names the volume; True for every volume while it cannot be read.

    The one answer every path that captures into, frees, restores, rebuilds,
    exports, deletes, or moves records or images into or out of a volume
    asks (module header).
    """
    if collection_id is None:
        return False
    try:
        journal = read_journal()
    except JournalUnreadable:
        return True
    return journal is not None and collection_id in (journal.get("source_id"), journal.get("target_id"))


def journaled_volume_ids() -> Optional[set]:
    """The ids of the volumes an adoption's journal names, read once; None while it cannot be read (it names every one).

    One open of the journal and no other look at the disk: the volume list
    asks it once for all its volumes.
    """
    try:
        data = json.loads(journal_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Only an integer id names a volume: a malformed one (a list, a string, missing) marks none.
    return {cid for cid in (data.get("source_id"), data.get("target_id"))
            if isinstance(cid, int) and not isinstance(cid, bool)}


STORAGE_RUNNING_MESSAGE = ("An adoption of captures, its recovery or its repair is running now, so the storage "
                           "is not switched. Try again when it has ended.")
STORAGE_UNFINISHED_MESSAGE = ("An adoption of captures is unfinished, and its files are on the active storage, so "
                              "the storage is not switched until it is finished or repaired.")


def journal_projects_root(journal) -> Optional[Path]:
    """The projects root a journal's files are on (its target root's parent), or None for a missing or malformed value.

    Only a string holding an absolute path counts. The result is compared,
    never written to or deleted under.
    """
    target_root = journal.get("target_root") if isinstance(journal, dict) else None
    if not isinstance(target_root, str) or not os.path.isabs(target_root):
        return None
    return Path(target_root).parent


def _same_root(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return False


def journal_root_is_active(journal) -> bool:
    """Whether the journal's projects root, valid, is the projects root in use now, compared after resolving."""
    root = journal_projects_root(journal)
    return root is not None and _same_root(root, config.settings.projects_dir)


@contextmanager
def storage_switch_guard(resulting_root: Optional[Path] = None):
    """Hold ADOPTION_LOCK across a change of the projects root; 409 while an adoption is running or unfinished.

    Taken without waiting: a held lock is an adoption, a recovery or a
    repair, `adoption_running`. Under the lock, a journal is
    `adoption_unfinished`, except for a switch whose resulting projects root
    (`resulting_root`) is, after resolving, the root of a readable journal
    with a valid one (journal_projects_root): activating the storage the
    unfinished adoption is on, which is how a repair becomes possible again.
    No adoption, recovery or repair can start while the switch holds the
    lock, and each of them takes it before it reads the root, so none runs
    across a switch.
    """
    from fastapi import HTTPException

    if not ADOPTION_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail={"code": "adoption_running",
                                                     "message": STORAGE_RUNNING_MESSAGE})
    try:
        if journal_exists():
            try:
                journal = read_journal()
            except JournalUnreadable:
                journal = None
            root = journal_projects_root(journal) if journal else None
            if resulting_root is None or root is None or not _same_root(resulting_root, root):
                raise HTTPException(status_code=409, detail={"code": "adoption_unfinished",
                                                             "message": STORAGE_UNFINISHED_MESSAGE})
        yield
    finally:
        ADOPTION_LOCK.release()


def refuse_if_journaled(collection_ids: Iterable[Optional[int]]) -> None:
    """409 `adoption_unfinished` when an adoption's journal names any of the volumes."""
    from fastapi import HTTPException

    if any(journal_names_volume(cid) for cid in collection_ids):
        raise HTTPException(status_code=409, detail={"code": "adoption_unfinished",
                                                     "message": unfinished_message()})


ADOPTED_AWAY = "captures_adopted_away"
ADOPTED_AWAY_MESSAGE = ("This volume's captures were moved into another volume. It takes no new captures or "
                        "records; delete it.")


def refuse_if_adopted_away(db, collection_ids: Iterable[Optional[int]]) -> None:
    """409 `captures_adopted_away` when any of the volumes is closed by an adoption (Collection.captures_adopted_at).

    Read with a statement of its own, so the value committed when the
    caller took the volume's row lock is the one read, not one held in the
    session. Each caller reads it after that lock, or, for a record's image,
    after the record's lock, which an adoption holds on every record of its
    source when it sets the column.
    """
    from fastapi import HTTPException

    from app.models.collection import Collection

    ids = [cid for cid in collection_ids if cid is not None]
    if not ids:
        return
    closed = db.query(Collection.id).filter(Collection.id.in_(ids), Collection.captures_adopted_at.isnot(None)).first()
    if closed is not None:
        raise HTTPException(status_code=409, detail={"code": ADOPTED_AWAY, "message": ADOPTED_AWAY_MESSAGE})


def _rename(src: Path, dst: Path) -> None:
    os.rename(src, dst)


def _append_lines(volume_root: Path, lines: list) -> None:
    from capture.manifestHandler import append_manifest_lines

    append_manifest_lines(volume_root, [json.dumps(line, ensure_ascii=False) for line in lines])


def _canonical(line: dict) -> str:
    """A manifest line's identity: its whole content, as JSON with sorted keys."""
    return json.dumps(line, sort_keys=True, ensure_ascii=False)


def _lines_missing(target_root: Path, lines: list) -> list:
    """The journal's target lines the target's manifest does not hold yet.

    A manifest line is identified by its whole content (_canonical): two
    lines of one capture id, such as two upload records a client gave the
    same capture id, are two lines, and a line already there is not
    appended again.
    """
    from app.core.integrity import read_manifest_lines

    manifest = target_root / "metadata" / "manifest.jsonl"
    present = set()
    if manifest.exists():
        for text in read_manifest_lines(manifest)[0]:
            try:
                entry = json.loads(text) if text.strip() else None
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                present.add(_canonical(entry))
    return [line for line in lines if _canonical(line) not in present]


def _card_rows(db: Session, journal: dict) -> tuple:
    """({shot id: (file_path, preview_path)}, {set id: collection_id}) for the journal's colour card rows, read now."""
    from app.models.colour_card import ColourCardSet, ColourCardShot

    shot_ids = [sh["id"] for sh in journal.get("colour_card_shots") or []]
    set_ids = list(journal.get("colour_card_set_ids") or [])
    shots = {sid: (fp, pp) for sid, fp, pp in db.query(ColourCardShot.id, ColourCardShot.file_path,
                                                        ColourCardShot.preview_path)
             .filter(ColourCardShot.id.in_(shot_ids))} if shot_ids else {}
    sets = dict(db.query(ColourCardSet.id, ColourCardSet.collection_id)
                .filter(ColourCardSet.id.in_(set_ids)).all()) if set_ids else {}
    return shots, sets


def recover_adoption_at_startup(db: Session) -> Optional[dict]:
    """Finish or undo the adoption a journal records (module header); None when there is none.

    Returns {"outcome": "completed" | "reverted" | "unfinished", "reason"}, and
    for "unfinished" its `code` (UNFINISHED_CODES).
    Never raises for a recovery that fails: the journal is kept, and an
    ERROR and an audit event say so.
    """
    with ADOPTION_LOCK:
        # What a killed repair staged goes first, with or without a journal (app.core.adopt_repair); a sweep
        # that fails is logged and startup goes on.
        try:
            from app.core.adopt_repair import sweep_staging_at_startup

            sweep_staging_at_startup()
        except Exception:
            logger.exception("Adoption repair: the staging sweep at startup failed; startup goes on")
        return recover_holding_lock(db)


def recover_holding_lock(db: Session) -> Optional[dict]:
    """recover_adoption_at_startup for a caller that already holds ADOPTION_LOCK (app.core.adopt_repair)."""
    global _last_recovery

    run_id = None
    try:
        journal = read_journal()
    except JournalUnreadable as exc:
        result = _unfinished(db, None, str(exc), UNREADABLE)
    else:
        if journal is None:
            return None
        run_id = journal.get("run_id")
        try:
            result = _recover(db, journal)
        except Exception as exc:
            logger.exception("Adoption recovery failed; the journal is kept")
            result = _unfinished(db, journal, f"recovery failed: {exc}", FAILED)
    _last_recovery = {"run_id": run_id, **result}
    return result


def decide(db: Session, journal: dict) -> tuple:
    """(landed, problem) from the journal's rows, read now; no row locked, nothing written.

    `landed` is True when the records are all in the target, False when all
    in the source, None when neither. `problem` is (code, reason) for the
    first row check that fails (module header), or None. The session is
    rolled back after the reads.
    """
    from app.models.record import Record, RecordImage

    # The records decide (module header).
    record_ids = journal.get("record_ids") or []
    homes = dict(db.query(Record.id, Record.collection_id).filter(Record.id.in_(record_ids)).all()) \
        if record_ids else {}
    images = journal.get("images") or []
    ids = [i["id"] for i in images]
    rows = dict(db.query(RecordImage.id, RecordImage.file_path).filter(RecordImage.id.in_(ids)).all()) if ids else {}
    shot_rows, set_homes = _card_rows(db, journal)
    db.rollback()
    new = bool(record_ids) and all(homes.get(r) == journal.get("target_id") for r in record_ids)
    old = bool(record_ids) and all(homes.get(r) == journal.get("source_id") for r in record_ids)
    if new == old:
        return None, (RECORDS_SPLIT, "the journal's records are not all in the target nor all in the source")
    wanted = "new_file_path" if new else "old_file_path"
    contradicting = [i["id"] for i in images if rows.get(i["id"]) != i[wanted]]
    if contradicting:
        return new, (IMAGE_ROWS, f"the rows of images {contradicting[:10]} contradict where the records are")
    side = "new" if new else "old"
    home = journal.get("target_id") if new else journal.get("source_id")
    shots_off = [sh["id"] for sh in journal.get("colour_card_shots") or []
                 if shot_rows.get(sh["id"]) != (sh[f"{side}_file_path"], sh[f"{side}_preview_path"])]
    sets_off = [sid for sid in journal.get("colour_card_set_ids") or [] if set_homes.get(sid) != home]
    if shots_off or sets_off:
        return new, (CARD_ROWS, f"the colour card shots {shots_off[:10]} or sets {sets_off[:10]} contradict "
                                "where the records are")
    return new, None


def _recover(db: Session, journal: dict) -> dict:
    new, problem = decide(db, journal)
    if problem is not None:
        return _unfinished(db, journal, problem[1], problem[0])
    old = not new

    moves = [(Path(m["old"]), Path(m["new"])) for m in journal.get("moves") or []]
    todo = []
    for before, after in moves:
        at_old, at_new = os.path.lexists(before), os.path.lexists(after)
        if at_old == at_new:
            where = "both" if at_old else "neither"
            return _unfinished(db, journal, f"{before.name} is at {where} of its old and new paths",
                               AT_BOTH if at_old else AT_NEITHER)
        if new and at_old:
            todo.append((before, after))
        elif old and at_new:
            todo.append((after, before))

    # Whatever this invocation renamed, every directory the journal names is made durable before the next
    # step: an earlier recovery may have renamed and been killed before it synced.
    target_root = Path(journal["target_root"])
    if new:
        for src, dst in todo:
            _rename(src, dst)
        sync_journal_directories(journal)
        missing = _lines_missing(target_root, journal.get("target_lines") or [])
        if missing:
            _append_lines(target_root, missing)
        sync_journal_directories(journal)
        remove_journal()
        return _done(db, journal, RECOVERY_COMPLETED_ACTION, "INFO", "completed",
                     f"renamed={len(todo)} lines_appended={len(missing)}")
    for src, dst in reversed(todo):
        _rename(src, dst)
    for directory in journal.get("created_dirs") or []:
        try:
            Path(directory).rmdir()
        except OSError:
            pass  # not empty, or already gone
    sync_journal_directories(journal)
    remove_journal()
    return _done(db, journal, RECOVERY_REVERTED_ACTION, "WARN", "reverted", f"put_back={len(todo)}")


def _subject(journal: Optional[dict]) -> Optional[str]:
    return journal.get("target_stem") if journal else None


def _done(db: Session, journal: dict, action: str, level: str, outcome: str, counts: str) -> dict:
    from app.core import audit

    detail = (f"source={journal.get('source_id')} ({journal.get('source_stem')}) "
              f"target={journal.get('target_id')} {counts}")
    logger.warning(f"Adoption recovery: {outcome}: {detail}")
    audit.log_event(db, level=level, category=CATEGORY, action=action, actor=journal.get("actor"),
                    subject=_subject(journal), detail=detail)
    return {"outcome": outcome, "reason": counts}


def _unfinished(db: Session, journal: Optional[dict], reason: str, code: str) -> dict:
    from app.core import audit

    path = journal_file()
    logger.error(f"Adoption recovery: nothing was changed, and {path} is kept for repair by hand: {reason}")
    detail = f"journal kept at {path}, repair by hand: {reason}"
    audit.log_event(db, level="ERR", category=CATEGORY, action=RECOVERY_UNFINISHED_ACTION,
                    actor=journal.get("actor") if journal else None, subject=_subject(journal),
                    detail=detail[:500])
    return {"outcome": "unfinished", "reason": reason, "code": code}
