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
- `testing_enabled`: whether an admin has switched the testing space on
  (app.core.testing_space).
- `testing_user_id`: the unit's one testing account; set null if that
  account is deleted.
- `testing_expiry_days`: how many days a testing volume is kept before it
  is due to be removed after its last capture; at least 1
  (check_unit_settings_testing_expiry_days).
- `testing_reserve_gb`: the free space, in GB of 10^9 bytes, kept for real
  work on the file system that holds the active projects root; below it
  the testing account's captures are refused (app.core.testing_space,
  refuse_below_reserve). 1 to 10000 (check_unit_settings_testing_reserve_gb).
- `default_language`: the unit's language, `es` or `en`
  (check_unit_settings_default_language), `es` until set: the language of
  the pages shown before anyone signs in, and the one the first account
  takes. Set by the registration that creates the first account and by an
  administrator (PUT /system/unit).
- `capture_key`: the key that starts a capture on this unit, one chord
  `{key, ctrl, alt, meta, shift}` (app.api.system.CaptureChord), or
  null for the frontend's built-in default. Set by an administrator or an
  operator (PUT /system/unit/capture-key).
- `colour_card_shots`: whether operators are asked for colour card shots
  in a volume (app.core.colour_card). False on a unit that updates to the
  release that adds it; set true by the registration that creates the
  first account, and by an administrator (PUT /system/unit).
- `updated_at`, `updated_by`: the last change, and the username that made it
  (null for the unit itself).

`id` is always 1 (check_unit_settings_single_row).

Version: 0.3.0
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, false, text

from app.core.db import Base

MODES = ("offline", "backup", "fisqua")
MODE_CHECK = "mode IN ('offline','backup','fisqua')"
SINGLE_ROW_CHECK = "id = 1"
TESTING_EXPIRY_CHECK = "testing_expiry_days >= 1"
DEFAULT_TESTING_EXPIRY_DAYS = 30
TESTING_RESERVE_CHECK = "testing_reserve_gb >= 1 AND testing_reserve_gb <= 10000"
DEFAULT_TESTING_RESERVE_GB = 20
LANGUAGES = ("es", "en")
DEFAULT_LANGUAGE = "es"
DEFAULT_LANGUAGE_CHECK = "default_language IN ('es','en')"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UnitSettings(Base):
    __tablename__ = "unit_settings"

    id = Column(Integer, primary_key=True, autoincrement=False)
    mode = Column(String(16), nullable=False)
    projects_enabled = Column(Boolean, nullable=False, default=False, server_default=false())
    default_project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    testing_enabled = Column(Boolean, nullable=False, default=False, server_default=false())
    testing_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL",
                                                 name="unit_settings_testing_user_id_fkey"), nullable=True)
    testing_expiry_days = Column(Integer, nullable=False, default=DEFAULT_TESTING_EXPIRY_DAYS,
                                 server_default=text(str(DEFAULT_TESTING_EXPIRY_DAYS)))
    testing_reserve_gb = Column(Integer, nullable=False, default=DEFAULT_TESTING_RESERVE_GB,
                                server_default=text(str(DEFAULT_TESTING_RESERVE_GB)))
    default_language = Column(String(2), nullable=False, default=DEFAULT_LANGUAGE,
                              server_default=DEFAULT_LANGUAGE)
    capture_key = Column(JSON, nullable=True)
    colour_card_shots = Column(Boolean, nullable=False, default=False, server_default=false())
    updated_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_by = Column(String(150), nullable=True)

    __table_args__ = (
        CheckConstraint(MODE_CHECK, name="check_unit_settings_mode"),
        CheckConstraint(SINGLE_ROW_CHECK, name="check_unit_settings_single_row"),
        CheckConstraint(TESTING_EXPIRY_CHECK, name="check_unit_settings_testing_expiry_days"),
        CheckConstraint(TESTING_RESERVE_CHECK, name="check_unit_settings_testing_reserve_gb"),
        CheckConstraint(DEFAULT_LANGUAGE_CHECK, name="check_unit_settings_default_language"),
    )
