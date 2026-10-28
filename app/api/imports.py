"""Routes for importing Fisqua exchange artefacts (contract 1.1).

POST /imports/digitisation-list takes the list as a multipart file upload,
the form the contract delivers it in (a file moved by download, USB or LAN
copy), so the interface can send the file the operator picked unchanged.
GET /imports/digitisation-list/{list_id}/report returns the latest stored
import report for a list, and GET /imports/digitisation-lists every imported
list with its items and the volume each one names. The import itself is
app/core/list_import.py.

Version: 0.2.0
"""

import hashlib
import json
import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, StrictBool
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.activity import status_counts
from app.api.auth import RoleChecker
from app.api.deps import get_db_dependency
from app.core.audit import log_event
from app.core.list_import import (
    ListHashNotRecorded,
    ProjectLockUnavailable,
    ListIdReused,
    ListUnverifiable,
    ListRefused,
    ListItemNotFound,
    ListUnreadable,
    ProjectNeedsAdmin,
    import_list,
    parse_list_file,
    latest_report,
    report_filename,
    set_done_mark,
    stem_is_well_formed,
    stored_lists,
)
from app.models.collection import Collection
from app.models.user import User

router = APIRouter()
logger = logging.getLogger(__name__)

# Creating volumes is admin or operator, as POST /collections. Creating a
# project is admin only, as POST /projects, so an operator can import only
# into a project that already has the list's name (import_list refuses the
# rest with ProjectNeedsAdmin).
allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])
# Confirming that a list item's work is finished is a review decision, as
# approving a record is (allow_reviewer in app/api/records.py).
allow_reviewer = RoleChecker(["admin", "reviewer"])

# A list carries a few hundred bytes per item; 10 MB is tens of thousands of
# items, far beyond a basket, and keeps a stray large file off the Pi's memory.
MAX_LIST_BYTES = 10 * 1024 * 1024
_READ_CHUNK = 64 * 1024


def _read_capped(upload: UploadFile) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = upload.file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_LIST_BYTES:
            raise HTTPException(status_code=413, detail="The digitisation list is larger than 10 MB.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/digitisation-list", status_code=200)
def import_digitisation_list(
    file: UploadFile = File(..., description="A digitisation-list JSON file (exchange contract 1.1, artefact 1)"),
    current_user: User = Depends(allow_contributor),
    db: Session = Depends(get_db_dependency),
):
    """Create one volume per item of a digitisation list, and return the import report.

    A file whose envelope this unit does not read is refused with a 422 and
    nothing is written. A list whose project does not exist yet needs an
    admin: from an operator it is refused with a 403 and nothing is written.
    A list id already imported from a file with a different SHA-256, or
    whose earlier import's hash cannot be read, is refused with a 409; if
    the list's hash cannot be recorded, with a 503. Nothing is written in
    either case. Otherwise the response is
    the report: counts
    `created` and `skipped`, the volumes created, and the `rejects`, each
    with the item's position, referenceCode and reason. `reportStored` says
    whether the report was also kept on the unit for
    GET /imports/digitisation-list/{list_id}/report, and `listStored`
    whether the uploaded file was kept for GET /imports/digitisation-lists.
    """
    raw = _read_capped(file)
    try:
        document = parse_list_file(raw)
    except ListUnreadable as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        report = import_list(db, document, current_user.username,
                             may_create_project=current_user.role == "admin",
                             file_sha256=hashlib.sha256(raw).hexdigest(), raw=raw)
    except ListRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ProjectNeedsAdmin as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (ListIdReused, ListUnverifiable) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ListHashNotRecorded, ProjectLockUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    stored = report["reportStored"]

    log_event(
        db, level="INFO" if stored else "WARN", category="activity",
        action="digitisation_list_imported", actor=current_user.username,
        subject=report["listName"],
        detail=(f"list {report['listId']}: {report['created']} created, "
                f"{report['skipped']} skipped" + ("" if stored else "; report not stored")),
    )
    return JSONResponse(content=report)


@router.get("/digitisation-lists")
def list_digitisation_lists(
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """Every imported list, newest import first, with its items in list order.

    Each list has `listId`, `listName`, `projectId`, `importedAt`,
    `listFileMissing` and `items`. Each item has `position`, `sequence`,
    `referenceCode`, `filenameStem`, `title`, `notes` (as the file has
    them) and `action` (null when absent), plus `collectionId`, the volume
    on this unit whose filename_stem is the item's filenameStem, and
    `status`, that volume's record counts by status as
    GET /activity/collections gives them; both null when there is no such
    volume, when the stem is not one the import accepts (it is then not
    looked up), and for an item that repeats a stem an earlier item of the
    same list, by position in the file, already gave. `projectId` is that
    volume's own project_id, null with it. `doneMark` is {by, at} when a
    person has marked the item done
    (PUT /imports/digitisation-lists/{list_id}/items/{position}/done),
    otherwise null; a missing or damaged done.json reads as no marks. A
    list whose stored file is missing or unreadable, which is every list
    imported before the file was kept, has `items: []` and
    `listFileMissing: true`. Reads only.
    """
    lists = stored_lists()
    # A stem the import would refuse is never bound: psycopg refuses a NUL
    # in a parameter, which would fail the whole response.
    stems = {item["filenameStem"] for entry in lists for item in entry["items"]
             if stem_is_well_formed(item["filenameStem"])}
    volume_of: dict[str, tuple[int, int | None]] = {}
    if stems:
        volume_of = {
            stem: (cid, pid)
            for stem, cid, pid in db.query(Collection.filename_stem, Collection.id, Collection.project_id)
            .filter(Collection.filename_stem.in_(stems))
            .all()
        }
    counts = status_counts(db, sorted({cid for cid, _ in volume_of.values()}))
    for entry in lists:
        marks = entry.pop("doneMarks")
        first_position: dict[str, int] = {}
        for item in entry["items"]:
            stem = item["filenameStem"]
            if stem_is_well_formed(stem):
                first_position[stem] = min(first_position.get(stem, item["position"]), item["position"])
        for item in entry["items"]:
            stem = item["filenameStem"]
            first = stem_is_well_formed(stem) and first_position[stem] == item["position"]
            cid, pid = volume_of.get(stem, (None, None)) if first else (None, None)
            item["collectionId"] = cid
            item["projectId"] = pid
            item["status"] = counts[cid].model_dump() if cid is not None else None
            item["doneMark"] = marks.get(item["position"])
    return JSONResponse(content=lists)


class DoneRequest(BaseModel):
    done: StrictBool


@router.put("/digitisation-lists/{list_id}/items/{position}/done")
def mark_list_item_done(
    list_id: str,
    position: int,
    payload: DoneRequest,
    current_user: User = Depends(allow_reviewer),
    db: Session = Depends(get_db_dependency),
):
    """Mark a list item done by hand, or with `done: false` remove the mark.

    Record counts cannot tell whether a spot recapture, a rescan or a
    completion is finished, so a person confirms it. `position` is the
    item's position in the stored list file, as GET
    /imports/digitisation-lists gives it. Returns `listId`, `position` and
    `doneMark` ({by, at}, or null once removed). A 422 when the list id is
    not a UUID or the body is not {"done": true|false}; a 404 when no
    stored list file of this id has an item at that position; a 503 when
    the mark cannot be written, in which case nothing has changed.
    """
    try:
        list_id = str(uuid.UUID(list_id))
    except ValueError:
        raise HTTPException(status_code=422, detail="The list id is not a UUID.")
    try:
        item, mark = set_done_mark(list_id, position, payload.done, current_user.username)
    except ListItemNotFound:
        raise HTTPException(status_code=404, detail="No stored list of this id has an item at this position.")
    except OSError:
        logger.exception("Could not write the done marks of list %s", list_id)
        raise HTTPException(status_code=503, detail="Captua could not save the mark on the unit. Try again.")

    found = latest_report(list_id)
    list_name = None
    if found is not None:
        try:
            list_name = json.loads(found[1].decode("utf-8")).get("listName")
        except ValueError:
            pass
    log_event(
        db, level="INFO", category="activity",
        action="digitisation_list_item_marked_done" if payload.done else "digitisation_list_item_unmarked",
        actor=current_user.username,
        subject=list_name,
        detail=f"list {list_id}: item {position} ({item['referenceCode']})",
    )
    return {"listId": list_id, "position": position, "doneMark": mark}


@router.get("/digitisation-list/{list_id}/report")
def get_import_report(
    list_id: str,
    current_user: User = Depends(allow_read_only),
):
    """The latest stored import report for a list, as `import-report_<list-id>.json`.

    The highest-numbered run that parses as the list's report; runs that
    do not are skipped (and logged). A 404 if none does.
    """
    try:
        found = latest_report(list_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="The list id is not a UUID.")
    if found is None:
        raise HTTPException(status_code=404, detail="No readable import report is stored for this list.")
    path, data = found
    filename = report_filename(path.parent.name)
    return Response(content=data, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
