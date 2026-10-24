"""Routes for importing Fisqua exchange artefacts (contract 1.1).

POST /imports/digitisation-list takes the list as a multipart file upload,
the form the contract delivers it in (a file moved by download, USB or LAN
copy), so the interface can send the file the operator picked unchanged.
GET /imports/digitisation-list/{list_id}/report returns the latest stored
import report for a list. The import itself is app/core/list_import.py.
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker
from app.api.deps import get_db_dependency
from app.core.audit import log_event
from app.core.list_import import (
    ListRefused,
    ListUnreadable,
    ProjectNeedsAdmin,
    import_list,
    parse_list_file,
    latest_report_path,
    report_filename,
    store_report,
)
from app.models.user import User

router = APIRouter()
logger = logging.getLogger(__name__)

# Creating volumes is admin or operator, as POST /collections. Creating a
# project is admin only, as POST /projects, so an operator can import only
# into a project that already has the list's name (import_list refuses the
# rest with ProjectNeedsAdmin).
allow_contributor = RoleChecker(["admin", "operator"])
allow_read_only = RoleChecker(["admin", "operator", "reviewer"])

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
    admin: from an operator it is refused with a 403 and nothing is written. Otherwise the response is the report: counts
    `created` and `skipped`, the volumes created, and the `rejects`, each
    with the item's position, referenceCode and reason. `reportStored` says
    whether the report was also kept on the unit for
    GET /imports/digitisation-list/{list_id}/report.
    """
    raw = _read_capped(file)
    try:
        document = parse_list_file(raw)
    except ListUnreadable as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        report = import_list(db, document, current_user.username,
                             may_create_project=current_user.role == "admin")
    except ListRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ProjectNeedsAdmin as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    try:
        store_report(report)
        stored = True
    except OSError:
        logger.exception("Could not store the import report for list %s", report["listId"])
        stored = False

    log_event(
        db, level="INFO" if stored else "WARN", category="activity",
        action="digitisation_list_imported", actor=current_user.username,
        subject=report["listName"],
        detail=(f"list {report['listId']}: {report['created']} created, "
                f"{report['skipped']} skipped" + ("" if stored else "; report not stored")),
    )
    return JSONResponse(content={**report, "reportStored": stored})


@router.get("/digitisation-list/{list_id}/report")
def get_import_report(
    list_id: str,
    current_user: User = Depends(allow_read_only),
):
    """The latest stored import report for a list, as `import-report_<list-id>.json`."""
    try:
        path = latest_report_path(list_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="The list id is not a UUID.")
    if path is None:
        raise HTTPException(status_code=404, detail="No import report is stored for this list.")
    canonical = path.parent.name
    return FileResponse(path, media_type="application/json", filename=report_filename(canonical))
