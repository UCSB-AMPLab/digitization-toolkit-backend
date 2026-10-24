"""Import a Fisqua digitisation list (exchange contract 1.1, artefact 1).

The list becomes a Project named after the basket (`list.name`), and each
item becomes a volume (a row of `collections`):

    referenceCode  -> external_id
    filenameStem   -> filename_stem (which must equal referenceCode)
    title          -> name
    dates.display  -> date
    dates.start    -> date_start
    dates.end      -> date_end
    sequence       -> sequence

Every other item field, `containerPath` and fields the contract does not
name included, goes into `custom_attributes` as JSON under a `fisqua` key;
`dates` subfields other than display, start and end go under `fisqua.dates`,
which is left out when there are none.
No records are created.

The envelope is checked before anything is written: a file whose `contract`,
`kind` or major version is not this one is refused whole (ListRefused), as is
one without the `id`, `list.name` or `items` the import needs. Unknown fields
anywhere are ignored by the checks.

Items are skip-and-report. Each one is checked, then created and committed on
its own, so one bad item never rolls back another, and a crash partway leaves
only whole volumes. An item that cannot be created goes into `rejects` with
its position, its referenceCode (None when it has none) and a reason. Nothing
is made up for a missing field. Re-importing a list rejects every item that
is already on the unit, by its reference code or its stem, so nothing is
created twice.

Volumes are created through the Collection model, so its guards and the
table's constraints apply; an IntegrityError at commit (a duplicate written
by a concurrent request between the check and the insert, or a CHECK) is
reported as a reject, not raised.

The report is returned and also written under
`<DTK_DATA_DIR>/imports/<list-id>/`, one numbered file per run, so an
operator can fetch it again (latest_report_path). Each report records the
SHA-256 of the uploaded file, and a list id already imported from a file
with a different hash is refused (ListIdReused).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

# The module, not the settings object, so that a reloaded config is seen.
from app.core import config
from app.core.db_errors import constraint_name
from app.models.collection import (
    FILENAME_STEM_MAX_LENGTH,
    FILENAME_STEM_PATTERN,
    Collection,
)
from app.models.project import Project

logger = logging.getLogger(__name__)

CONTRACT_NAME = "fisqua-captua-exchange"
LIST_KIND = "digitisation-list"
SUPPORTED_MAJOR_VERSION = "1"

# The item fields that have a column; everything else goes under `fisqua`.
MAPPED_ITEM_FIELDS = frozenset({"referenceCode", "filenameStem", "title", "dates", "sequence"})
# The `dates` subfields that have a column; any other goes under fisqua.dates.
MAPPED_DATES_FIELDS = frozenset({"display", "start", "end"})

# Column widths the values must fit (app/models/collection.py, project.py).
_NAME_MAX_LENGTH = Collection.__table__.columns["name"].type.length
_EXTERNAL_ID_MAX_LENGTH = Collection.__table__.columns["external_id"].type.length
_DATE_MAX_LENGTH = Collection.__table__.columns["date"].type.length
_PROJECT_NAME_MAX_LENGTH = Project.__table__.columns["name"].type.length

# collections.sequence is an Integer, which is int4 on Postgres.
_SEQUENCE_MIN, _SEQUENCE_MAX = -(2**31), 2**31 - 1

_STEM_RE = re.compile(FILENAME_STEM_PATTERN)
# date.fromisoformat also takes forms such as 17900101; the contract's dates
# are ISO calendar dates written out in full.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Reason codes in a reject, with the message that goes beside each.
REASONS = {
    "item_not_an_object": "The item is not a JSON object.",
    "missing_reference_code": "The item has no referenceCode.",
    "invalid_reference_code": f"The referenceCode is not a string of at most {_EXTERNAL_ID_MAX_LENGTH} characters.",
    "reference_code_padded": "The referenceCode begins or ends with whitespace.",
    "reference_code_duplicated_in_list": "An earlier item in this list has the same referenceCode.",
    "missing_filename_stem": "The item has no filenameStem.",
    "filename_stem_not_a_slug": "The filenameStem is not lowercase letters and digits separated by single hyphens.",
    "filename_stem_too_long": f"The filenameStem is longer than {FILENAME_STEM_MAX_LENGTH} characters.",
    "filename_stem_differs_from_reference_code": "The filenameStem is not the same as the referenceCode.",
    "filename_stem_duplicated_in_list": "An earlier item in this list has the same filenameStem.",
    "missing_title": "The item has no title.",
    "invalid_title": f"The title is not a string of at most {_NAME_MAX_LENGTH} characters.",
    "invalid_dates": (
        f"The dates are not an object whose display is a string of at most {_DATE_MAX_LENGTH} "
        "characters and whose start and end are ISO dates (YYYY-MM-DD) or null."
    ),
    "end_date_before_start": "The end date is before the start date.",
    "invalid_sequence": f"The sequence is not an integer from {-(2**31)} to {2**31 - 1}.",
    "reference_code_on_unit": "A volume on this unit already has this referenceCode.",
    "filename_stem_on_unit": "A volume on this unit already has this filenameStem.",
    "refused_by_database": "The database refused the volume.",
}

# A constraint named in an IntegrityError at commit, mapped to its reason.
_CONSTRAINT_REASONS = {
    "ix_collections_external_id": "reference_code_on_unit",
    "collections_filename_stem_key": "filename_stem_on_unit",
    "check_collection_date_range": "end_date_before_start",
    "check_collection_filename_stem_pattern": "filename_stem_not_a_slug",
    "check_collection_filename_stem_length": "filename_stem_too_long",
    "check_collection_external_id_trimmed": "reference_code_padded",
}


class ListRefused(Exception):
    """The file as a whole cannot be imported; nothing has been written."""


class ListUnreadable(Exception):
    """The file cannot be read as one strict JSON document; nothing has been written."""


class _DuplicateKey(ValueError):
    pass


def _refuse_constant(name: str):
    # json accepts NaN, Infinity and -Infinity, which are not JSON (RFC 8259).
    raise ListUnreadable(f"The file is not valid JSON: it contains {name}, which JSON does not allow.")


def _refuse_duplicate_keys(pairs):
    # A key given twice in one object is read by json as its last value,
    # silently; a file whose first `version` was 2.0 would pass as 1.1.
    obj = {}
    for key, value in pairs:
        if key in obj:
            shown = key if len(key) <= 120 else key[:120] + "..."
            raise _DuplicateKey(shown)
        obj[key] = value
    return obj


def parse_list_file(raw: bytes) -> Any:
    """Decode an uploaded list as strict UTF-8 JSON, or raise ListUnreadable.

    A UTF-8 byte order mark is accepted. Refused: bytes that are not UTF-8;
    text that is not JSON, including a number beyond Python's digit limit
    and nesting deep enough to exhaust the recursion limit; NaN, Infinity
    and -Infinity; and a key repeated within one object, at any depth.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ListUnreadable("The file is not UTF-8 text.")
    try:
        return json.loads(text, parse_constant=_refuse_constant,
                          object_pairs_hook=_refuse_duplicate_keys)
    except _DuplicateKey as exc:
        raise ListUnreadable(f"The file is not valid for import: the key {str(exc)!r} appears twice in one object.")
    except RecursionError:
        raise ListUnreadable("The file is not valid JSON that can be read: it is nested too deeply.")
    except ValueError as exc:
        # json.JSONDecodeError, and the ValueError for an integer over the
        # interpreter's digit limit.
        raise ListUnreadable(f"The file is not valid JSON: {exc}")


class ProjectNeedsAdmin(Exception):
    """The list would create a project and the caller may not; nothing has been written."""


PROJECT_NEEDS_ADMIN_MESSAGE = (
    "No project of this list's name exists, and importing this list would create one. "
    "An admin must import a list that creates a project."
)


def _major_version(version: Any) -> Optional[str]:
    if not isinstance(version, str):
        return None
    major = version.split(".", 1)[0]
    return major if major.isdigit() else None


def check_envelope(document: Any) -> tuple[str, str, list]:
    """Refuse a file that is not a digitisation list this importer reads.

    Returns the list id (canonical UUID form), the list name and the items.
    Unknown envelope fields are ignored. The id is required because the
    report is named by it; the name, because the project is.
    """
    if not isinstance(document, dict):
        raise ListRefused("The file is not a JSON object.")
    if document.get("contract") != CONTRACT_NAME:
        raise ListRefused(f"The file's contract is not {CONTRACT_NAME}.")
    if document.get("kind") != LIST_KIND:
        raise ListRefused(f"The file's kind is not {LIST_KIND}.")
    if _major_version(document.get("version")) != SUPPORTED_MAJOR_VERSION:
        raise ListRefused(
            f"The file's contract version is not {SUPPORTED_MAJOR_VERSION}.x; "
            "this unit reads major version 1 only."
        )
    raw_id = document.get("id")
    try:
        list_id = str(uuid.UUID(raw_id)) if isinstance(raw_id, str) else None
    except ValueError:
        list_id = None
    if list_id is None:
        raise ListRefused("The file's id is not a UUID.")
    meta = document.get("list")
    name = meta.get("name") if isinstance(meta, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise ListRefused("The file has no list name, which the project is named after.")
    if len(name) > _PROJECT_NAME_MAX_LENGTH:
        raise ListRefused(f"The list name is longer than {_PROJECT_NAME_MAX_LENGTH} characters.")
    items = document.get("items")
    if not isinstance(items, list):
        raise ListRefused("The file's items are not a list.")
    return list_id, name, items


def _reference_code(item: dict) -> Any:
    """The item's referenceCode exactly as the file gives it; a blank string counts as missing.

    A code is never stripped: codes are exchanged as Fisqua canonicalises
    them, and Captua never modifies one, so a padded code is a reject
    (reference_code_padded), not a code to tidy.
    """
    code = item.get("referenceCode")
    if isinstance(code, str) and not code.strip():
        return None
    return code


def _parse_date(value: Any) -> tuple[bool, Optional[date]]:
    if value is None:
        return True, None
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False, None
    try:
        return True, date.fromisoformat(value)
    except ValueError:
        return False, None


def _first_positions(items: list, key) -> dict:
    """value -> 1-based position of its first item, for string values of `key(item)`."""
    first: dict = {}
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        value = key(item)
        if isinstance(value, str) and value not in first:
            first[value] = position
    return first


def _check_item(position: int, item: Any, first_code: dict, first_stem: dict) -> tuple[Optional[str], dict]:
    """The reject reason for an item, or None and the volume's column values.

    Checks run in a fixed order and the first failure is the reason given:
    the reference code's checks, then the stem's format and length, then
    that the stem equals the code, then the stem's duplicate in the list,
    then the title, dates and sequence.
    The checks against volumes already on the unit come after this, in
    import_list, since they read the database.
    """
    if not isinstance(item, dict):
        return "item_not_an_object", {}
    code = _reference_code(item)
    if code is None:
        return "missing_reference_code", {}
    if not isinstance(code, str) or len(code) > _EXTERNAL_ID_MAX_LENGTH:
        return "invalid_reference_code", {}
    if code != code.strip():
        return "reference_code_padded", {}
    if first_code.get(code) != position:
        return "reference_code_duplicated_in_list", {}

    stem = item.get("filenameStem")
    if stem is None or stem == "":
        return "missing_filename_stem", {}
    if not isinstance(stem, str) or not _STEM_RE.match(stem):
        return "filename_stem_not_a_slug", {}
    if len(stem) > FILENAME_STEM_MAX_LENGTH:
        return "filename_stem_too_long", {}
    # The stem is the volume's reference code, unchanged (contract 1.1 §8,
    # ruled 2026-10-23). Checked after the stem's own format, so a stem that
    # is not a slug or is too long is reported as that.
    if stem != code:
        return "filename_stem_differs_from_reference_code", {}
    # With the stem equal to the code, a stem repeated in the list has a
    # repeated code too, which is reported above; this check, and the
    # stem-on-unit check in import_list, stay for a stem held without that code.
    if first_stem.get(stem) != position:
        return "filename_stem_duplicated_in_list", {}

    title = item.get("title")
    if title is None or (isinstance(title, str) and not title.strip()):
        return "missing_title", {}
    if not isinstance(title, str) or len(title) > _NAME_MAX_LENGTH:
        return "invalid_title", {}

    dates = item.get("dates")
    if dates is None:
        dates = {}
    if not isinstance(dates, dict):
        return "invalid_dates", {}
    display = dates.get("display")
    if display is not None and (not isinstance(display, str) or len(display) > _DATE_MAX_LENGTH):
        return "invalid_dates", {}
    ok_start, start = _parse_date(dates.get("start"))
    ok_end, end = _parse_date(dates.get("end"))
    if not (ok_start and ok_end):
        return "invalid_dates", {}
    if start is not None and end is not None and end < start:
        return "end_date_before_start", {}

    sequence = item.get("sequence")
    # bool is an int subclass; true is not a sequence number.
    if sequence is not None and (
        isinstance(sequence, bool) or not isinstance(sequence, int)
        or not _SEQUENCE_MIN <= sequence <= _SEQUENCE_MAX
    ):
        return "invalid_sequence", {}

    fisqua = {k: v for k, v in item.items() if k not in MAPPED_ITEM_FIELDS}
    unmapped_dates = {k: v for k, v in dates.items() if k not in MAPPED_DATES_FIELDS}
    if unmapped_dates:
        fisqua["dates"] = unmapped_dates
    return None, {
        "external_id": code,
        "filename_stem": stem,
        "name": title,
        "date": display,
        "date_start": start,
        "date_end": end,
        "sequence": sequence,
        "custom_attributes": json.dumps({"fisqua": fisqua}, ensure_ascii=False),
    }


def _find_project(db: Session, name: str) -> Optional[Project]:
    """The project the list's name refers to, or None."""
    return db.query(Project).filter(Project.name == name).first()


def _project_for(db: Session, name: str, actor: Optional[str], may_create_project: bool) -> tuple[Project, bool]:
    """The project named `name`, created if there is none. Returns (project, created).

    An existing project of that name is reused: a project is a grouping in
    the catalogue only, and re-importing a list has to land in the project
    the first import made. Creating one needs `may_create_project`, as
    POST /projects needs an admin; without it, ProjectNeedsAdmin is raised
    before anything is written.
    """
    project = _find_project(db, name)
    if project is not None:
        return project, False
    if not may_create_project:
        raise ProjectNeedsAdmin(PROJECT_NEEDS_ADMIN_MESSAGE)
    project = Project(name=name, created_by=actor)
    db.add(project)
    try:
        db.commit()
    except IntegrityError:
        # Created by a concurrent request since the query above.
        db.rollback()
        project = _find_project(db, name)
        if project is None:
            raise
        return project, False
    db.refresh(project)
    return project, True


def _reject(position: int, code: Any, reason: str) -> dict:
    return {
        "position": position,
        "referenceCode": code if isinstance(code, str) else None,
        "reason": reason,
        "message": REASONS[reason],
    }


def _import_items(db: Session, list_id: str, list_name: str, items: list,
                  actor: Optional[str], may_create_project: bool) -> dict:
    """Find or create the project, then create each item's volume; return the report."""
    project, project_created = _project_for(db, list_name, actor, may_create_project)
    project_id = project.id

    first_code = _first_positions(items, _reference_code)
    first_stem = _first_positions(items, lambda item: item.get("filenameStem"))

    created: list[dict] = []
    rejects: list[dict] = []
    for position, item in enumerate(items, start=1):
        code = _reference_code(item) if isinstance(item, dict) else None
        reason, values = _check_item(position, item, first_code, first_stem)
        if reason is None:
            if db.query(Collection.id).filter(Collection.external_id == values["external_id"]).first():
                reason = "reference_code_on_unit"
            elif db.query(Collection.id).filter(Collection.filename_stem == values["filename_stem"]).first():
                reason = "filename_stem_on_unit"
        if reason is not None:
            rejects.append(_reject(position, code, reason))
            continue

        volume = Collection(project_id=project_id, created_by=actor, **values)
        db.add(volume)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            reason = _CONSTRAINT_REASONS.get(constraint_name(exc), "refused_by_database")
            rejects.append(_reject(position, code, reason))
            continue
        except SQLAlchemyError:
            # Any other database failure on this item (a lost connection, a
            # full disk, a value the driver cannot bind) costs this item
            # only: it is rolled back and reported, and the import goes on.
            logger.exception("Import of list %s: item %d refused by the database", list_id, position)
            db.rollback()
            rejects.append(_reject(position, code, "refused_by_database"))
            continue
        created.append({
            "position": position,
            "referenceCode": values["external_id"],
            "filenameStem": values["filename_stem"],
            "captuaCollectionId": volume.id,
        })

    return {
        "listId": list_id,
        "listName": list_name,
        "projectId": project_id,
        "projectName": list_name,
        "projectCreated": project_created,
        "importedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "importedBy": actor,
        "created": len(created),
        "skipped": len(rejects),
        "volumes": created,
        "rejects": rejects,
    }


class ListIdReused(Exception):
    """An earlier run of this list id had a file with a different SHA-256; nothing has been written."""


LIST_ID_REUSED_MESSAGE = "This list id was already imported with different content."


def import_list(db: Session, document: Any, actor: Optional[str], may_create_project: bool,
                file_sha256: Optional[str] = None) -> dict:
    """Import a digitisation list, store its report, and return it.

    Raises, before writing anything to the database or a report:
    - ListRefused, when the envelope is not one this importer reads;
    - ListIdReused, when an earlier stored run of the same list id records
      a `fileSha256` other than `file_sha256` (an identical file may be
      imported again);
    - ProjectNeedsAdmin, when no project matches the list's name and
      `may_create_project` is false.
    Otherwise commits the project (if new) and then each volume on its own,
    stores the report as the list's next run, and returns it with
    `reportStored` saying whether that write succeeded (the stored file
    does not carry that key).

    Imports of the same list id are serialised by an exclusive lock on
    `<reports_dir>/<list-id>/.lock`, held from the hash check to the
    stored report, so two concurrent runs cannot both pass the check. If
    the lock file cannot be created the import goes on unlocked and says
    so in the log; the run file is still created exclusively.
    """
    list_id, list_name, items = check_envelope(document)
    # Checked before the lock too, so that a refusal creates no lock file;
    # _project_for checks again under the lock.
    if not may_create_project and _find_project(db, list_name) is None:
        raise ProjectNeedsAdmin(PROJECT_NEEDS_ADMIN_MESSAGE)
    with _list_lock(list_id):
        if file_sha256 is not None:
            for earlier in _earlier_hashes(list_id):
                if earlier is not None and earlier != file_sha256:
                    raise ListIdReused(LIST_ID_REUSED_MESSAGE)
        report = _import_items(db, list_id, list_name, items, actor, may_create_project)
        report["fileSha256"] = file_sha256
        try:
            store_report(report)
            stored = True
        except OSError:
            logger.exception("Could not store the import report for list %s", list_id)
            stored = False
    return {**report, "reportStored": stored}


class _list_lock:
    """An exclusive flock on `<reports_dir>/<list-id>/.lock`, or nothing if it cannot be made."""

    def __init__(self, list_id: str):
        self.path = reports_dir() / list_id / ".lock"
        self.fd = None

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except OSError:
            logger.exception("Could not lock %s; importing without the lock", self.path)
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
        return False


def _run_files(directory: Path, list_id: str) -> list[tuple[int, Path]]:
    """(run number, path) for each stored run of a list, in run order."""
    runs = []
    for path in directory.glob(f"import-report_{list_id}_run*.json"):
        match = _RUN_RE.fullmatch(path.name[len(f"import-report_{list_id}_"):])
        if match:
            runs.append((int(match.group(1)), path))
    return sorted(runs)


_RUN_RE = re.compile(r"run(\d+)\.json")


def _earlier_hashes(list_id: str) -> list:
    """The fileSha256 of each stored run of a list; None for a run that cannot be read or has none."""
    hashes = []
    for _, path in _run_files(reports_dir() / list_id, list_id):
        try:
            hashes.append(json.loads(path.read_text(encoding="utf-8")).get("fileSha256"))
        except (OSError, ValueError, AttributeError):
            hashes.append(None)
    return hashes


def _next_run_number(directory: Path, list_id: str) -> int:
    runs = _run_files(directory, list_id)
    return runs[-1][0] + 1 if runs else 1


def reports_dir() -> Path:
    """Where import reports are kept: `<DTK_DATA_DIR>/imports`."""
    return config.settings.data_dir / "imports"


def report_filename(list_id: str) -> str:
    """The contract's name for the report, `import-report_<list-id>.json`."""
    return f"import-report_{list_id}.json"


def store_report(report: dict) -> Path:
    """Write the report as `<reports_dir>/<list-id>/import-report_<list-id>_run<NNNN>.json`.

    One file per run, numbered from 1, so a re-import's report does not
    replace the first one's, and the order does not depend on the clock.
    The report is written to a temporary file and hard-linked to the run's
    name, which fails if that name exists, so two runs never take the same
    number and a reader never sees half a report; on a taken number the
    next one is tried. `reportStored` is not written. Raises OSError if the
    report cannot be written.
    """
    list_id = str(uuid.UUID(report["listId"]))  # never a path component from the file unchecked
    directory = reports_dir() / list_id
    directory.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in report.items() if k != "reportStored"}
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        number = _next_run_number(directory, list_id)
        while True:
            target = directory / f"import-report_{list_id}_run{number:04d}.json"
            try:
                os.link(tmp, target)
                break
            except FileExistsError:
                number += 1
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return target


def latest_report_path(list_id: str) -> Optional[Path]:
    """The stored report with the highest run number for a list, or None. `list_id` must be a UUID."""
    list_id = str(uuid.UUID(list_id))
    runs = _run_files(reports_dir() / list_id, list_id)
    return runs[-1][1] if runs else None
