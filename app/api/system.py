import re
import subprocess

from fastapi import APIRouter, Depends

from app.api.auth import RoleChecker
from app.models.user import User

allow_read_only = RoleChecker(["admin", "operator", "reviewer"])

router = APIRouter()


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
