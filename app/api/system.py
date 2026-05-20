import re
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker
from app.api.deps import get_db_dependency
from app.models.system_log import SystemLog
from app.models.user import User
from app.schemas.system_log import SystemLogOut

allow_read_only = RoleChecker(["admin", "operator", "reviewer"])
allow_admin     = RoleChecker(["admin"])

router = APIRouter()


@router.get("/logs", response_model=list[SystemLogOut])
def get_system_logs(
    limit:    int            = Query(default=50, ge=1, le=500),
    category: Optional[str] = Query(default=None),
    level:    Optional[str] = Query(default=None),
    current_user: User    = Depends(allow_admin),
    db: Session           = Depends(get_db_dependency),
):
    """Return recent audit log entries, newest first. Admin-only."""
    query = db.query(SystemLog).order_by(SystemLog.created_at.desc())
    if category:
        query = query.filter(SystemLog.category == category)
    if level:
        query = query.filter(SystemLog.level == level)
    return query.limit(limit).all()


@router.get("/temperature")
def get_temperature(current_user: User = Depends(allow_read_only)):
    """Get Raspberry Pi CPU temperature via vcgencmd measure_temp.

    Returns temperature in Celsius, or available=False if vcgencmd is not
    present (e.g. development environment without camera hardware).
    """
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # Output format: temp=47.2'C
        match = re.search(r"temp=([\d.]+)", result.stdout)
        if match:
            temperature = float(match.group(1))
            return {"temperature": temperature, "unit": "C", "available": True}
        return {"temperature": None, "unit": "C", "available": False}
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return {"temperature": None, "unit": "C", "available": False}
