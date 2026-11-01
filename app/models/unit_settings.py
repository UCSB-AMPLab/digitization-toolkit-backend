"""The unit's own settings: one row, read and written only by app.core.unit_mode.

- `mode`: `offline` (nothing leaves the scanner), `backup` (images and the
  catalogue are copied to the bucket) or `fisqua` (also the link to
  Fisqua: handovers, status, decisions, the digitisation queue).
- `projects_enabled`: whether an admin has turned projects on. Projects are
  shown only in `offline` mode with this on; the value is kept whatever the
  mode.
- `default_project_id`: the project every new volume goes into while
  projects are hidden. Null until first needed; set null if that project
  is deleted, and chosen again at the next need.
- `updated_at`, `updated_by`: the last change, and the username that made it
  (null for the unit itself).

`id` is always 1 (check_unit_settings_single_row).

Version: 0.3.3
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, false

from app.core.db import Base

MODES = ("offline", "backup", "fisqua")
MODE_CHECK = "mode IN ('offline','backup','fisqua')"
SINGLE_ROW_CHECK = "id = 1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UnitSettings(Base):
    __tablename__ = "unit_settings"

    id = Column(Integer, primary_key=True, autoincrement=False)
    mode = Column(String(16), nullable=False)
    projects_enabled = Column(Boolean, nullable=False, default=False, server_default=false())
    default_project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_by = Column(String(150), nullable=True)

    __table_args__ = (
        CheckConstraint(MODE_CHECK, name="check_unit_settings_mode"),
        CheckConstraint(SINGLE_ROW_CHECK, name="check_unit_settings_single_row"),
    )
