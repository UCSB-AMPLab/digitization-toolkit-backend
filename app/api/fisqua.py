# Version: 0.3.3
"""Routes that ask Fisqua for the form, over the unit's link (app.core.unit_mode).

GET /fisqua/descriptions?q= searches Fisqua's descriptions (contract 1.10)
for the new-volume form's reference-code field, for the one role that can
create a volume in fisqua mode: an admin. The answers and refusals are unit_mode.search_descriptions's.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker
from app.api.deps import get_db_dependency
from app.core import fisqua_contract as fc
from app.core import unit_mode
from app.models.user import User

router = APIRouter()

# In fisqua mode only an admin can create a volume (app/api/collections.py,
# volumes_from_fisqua), so only an admin searches.
allow_admin = RoleChecker(["admin"])


@router.get("/descriptions")
def search_descriptions(
    q: Optional[str] = Query(default=None, max_length=1024),
    current_user: User = Depends(allow_admin),
    db: Session = Depends(get_db_dependency),
):
    """Fisqua's descriptions matching `q`: 200 {results} (or {results: [], search: false} from a Fisqua without 1.10).

    409 off_in_this_mode or fisqua_not_configured; 400 query_too_short;
    429 rate_limited, with Retry-After when there is one; 503
    fisqua_unreachable (app.core.unit_mode.search_descriptions).
    """
    try:
        return unit_mode.search_descriptions(db, q)
    except unit_mode.SearchRefused as exc:
        headers = {fc.RETRY_AFTER_HEADER: str(exc.retry_after)} if exc.retry_after is not None else None
        raise HTTPException(status_code=exc.status, detail=exc.detail(), headers=headers)
