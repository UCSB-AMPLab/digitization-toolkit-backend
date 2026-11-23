"""Colour card shots: photographs of a colour reference card, taken in a volume under the light and settings of its pages.

They are kept outside records (app.core.colour_card): no record, no image
row, no place in the page sequence, in pairs, in review or in retakes, and
no count. Their files are in the volume's `calibration/` folder
(app.core.paths.CALIBRATION_DIR).

**colour_card_sets**: one row per request for shots in a volume.

- `collection_id`: the volume; its sets go with it (ON DELETE CASCADE).
- `number`: the set's number in the volume, from 1, given when the set is
  opened or a request skipped; unique in the volume
  (`colour_card_sets_collection_id_number_key`). The shots' file names
  carry it (`<stem>-cal_<nn>_c<n>`).
- `outcome`: null while the set is open, then `taken` (every camera of
  `capture_mode` has its shot) or `skipped` (an operator skipped the
  request); `check_colour_card_set_outcome`. Null exactly while
  `completed_at` is null (`check_colour_card_set_completed`).
- `capture_mode`: `single` or `dual` (`check_colour_card_set_capture_mode`),
  which says which cameras must have a shot.
- `cameras`: the settings the request was made at, one entry per camera of
  the set, `{"camera_index", "hardware_id", "role", "settings"}`, `settings`
  null for a camera that could not be read. A later request compares the
  cameras' settings with these (app.core.colour_card.needed), for a skipped
  set as for a taken one.
- `opened_at`, `opened_by`: when the set was opened (or the request skipped),
  and the username.
- `completed_at`, `completed_by`: when and by whom it was marked taken or
  skipped.
- `after_image_id`: the highest record_images id on the unit when the set
  was completed. A page whose image id is above it was captured after the
  set. Not a foreign key: no image need have it any more.

**colour_card_shots**: one row per camera's shot in a set.

- `set_id`: the set; its shots go with it (ON DELETE CASCADE).
- `camera_index`, `hardware_id`, `role`: the camera as it was when the shot
  was taken: its index, its body's hardware id and its page role (`left`,
  `right`, or `single` in a single-camera set). One shot per camera in a
  set (`colour_card_shots_set_id_camera_index_key`); taking it again
  replaces the row's file and fields.
- `capture_id`: the shot's own id, a fresh uuid4 for every shot taken, and
  unique on the unit (`colour_card_shots_capture_id_key`): a record image
  never has it, and the two cameras' shots never share one.
- `filename`, `file_path`, `file_size`, `sha256`: the master as written,
  `<stem>-cal_<nn>_c<n>.<ext>` in `calibration/`, its digest taken when it
  was written.
- `preview_filename`, `preview_path`, `preview_sha256`: a RAW master's
  `_preview.jpg`, beside it in `calibration/`; null for a JPEG master.
- `rotate_deg`: the clockwise rotation that displays the shot upright
  (capture.camera.recorded_rotation), as for a page.
- `captured_at`, `captured_by`.
- `settings`: what the driver could read when the shot was taken
  (app.core.colour_card.shot_settings).

**colour_card_objects**: one row per object the offsite upload puts in the
bucket for a shot (`kind` `shot`, or `preview` for a RAW master's preview),
with the states of offsite_objects (app.models.offsite_object): pending,
verified (`verified_at`) or refused (`refused_at` and `refusal`). Its own
table, not offsite_objects, whose rows each name a record image and whose
readers (the volume's received state, freeing, the stem check) read a row
with no image as a deleted image's object. `object_key` is unique and never
changes. A shot's object stays in the bucket when the shot row goes, so the
row keeps its key (`shot_id` set null).

`last_skip_reason` and `skip_count` are the reason the uploader last skipped
the object for and how many consecutive runs skipped it for that reason
(app.core.image_upload._note_card_skip): `staging_failed`, `file_unstable`,
`listed_without_sha1` or `unverified`. At SKIP_LIMIT runs the object is
refused with that reason. A run that verifies it clears both. A row written
only to record a skip has no `local_sha1`: its bytes were never read, and
nothing of it left the unit.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, event, text
from sqlalchemy.orm import relationship

from app.core.db import Base
from app.models.record import ImmutableFieldError

OUTCOMES = ("taken", "skipped")
OUTCOME_CHECK = "outcome IS NULL OR outcome IN ('taken', 'skipped')"
COMPLETED_CHECK = "(outcome IS NULL) = (completed_at IS NULL)"
CAPTURE_MODES = ("single", "dual")
CAPTURE_MODE_CHECK = "capture_mode IN ('single', 'dual')"
OBJECT_KINDS = ("shot", "preview")
OBJECT_KIND_CHECK = "kind IN ('shot', 'preview')"
OBJECT_STATE_CHECK = "verified_at IS NULL OR refused_at IS NULL"
OBJECT_REFUSAL_CHECK = (
    "(refused_at IS NULL AND refusal IS NULL) OR "
    "(refused_at IS NOT NULL AND refusal IS NOT NULL AND refusal IN ('remote_differs', 'fixity', "
    "'staging_failed', 'file_unstable', 'listed_without_sha1', 'unverified'))")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ColourCardSet(Base):
    __tablename__ = "colour_card_sets"

    id = Column(Integer, primary_key=True)
    collection_id = Column(Integer, ForeignKey("collections.id", ondelete="CASCADE",
                                               name="colour_card_sets_collection_id_fkey"),
                           nullable=False, index=True)
    number = Column(Integer, nullable=False)
    outcome = Column(String(10), nullable=True)
    capture_mode = Column(String(10), nullable=False)
    cameras = Column(JSON, nullable=False)
    opened_at = Column(DateTime, nullable=False, default=_utcnow)
    opened_by = Column(String(150), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    completed_by = Column(String(150), nullable=True)
    after_image_id = Column(Integer, nullable=True)

    shots = relationship("ColourCardShot", back_populates="card_set", cascade="all, delete-orphan",
                         order_by="ColourCardShot.camera_index")

    __table_args__ = (
        UniqueConstraint("collection_id", "number", name="colour_card_sets_collection_id_number_key"),
        CheckConstraint(OUTCOME_CHECK, name="check_colour_card_set_outcome"),
        CheckConstraint(COMPLETED_CHECK, name="check_colour_card_set_completed"),
        CheckConstraint(CAPTURE_MODE_CHECK, name="check_colour_card_set_capture_mode"),
    )


class ColourCardShot(Base):
    __tablename__ = "colour_card_shots"

    id = Column(Integer, primary_key=True)
    set_id = Column(Integer, ForeignKey("colour_card_sets.id", ondelete="CASCADE",
                                        name="colour_card_shots_set_id_fkey"),
                    nullable=False, index=True)
    camera_index = Column(Integer, nullable=False)
    hardware_id = Column(String(255), nullable=True)
    role = Column(String(10), nullable=True)
    capture_id = Column(String(36), nullable=False)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(512), nullable=False)
    file_size = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=False)
    preview_filename = Column(String(255), nullable=True)
    preview_path = Column(String(512), nullable=True)
    preview_sha256 = Column(String(64), nullable=True)
    rotate_deg = Column(Integer, nullable=True)
    captured_at = Column(DateTime, nullable=False, default=_utcnow)
    captured_by = Column(String(150), nullable=True)
    settings = Column(JSON, nullable=True)

    card_set = relationship("ColourCardSet", back_populates="shots")

    __table_args__ = (
        UniqueConstraint("set_id", "camera_index", name="colour_card_shots_set_id_camera_index_key"),
        UniqueConstraint("capture_id", name="colour_card_shots_capture_id_key"),
    )


class ColourCardObject(Base):
    __tablename__ = "colour_card_objects"

    id = Column(Integer, primary_key=True)
    shot_id = Column(Integer, ForeignKey("colour_card_shots.id", ondelete="SET NULL",
                                         name="colour_card_objects_shot_id_fkey"),
                     nullable=True, index=True)
    kind = Column(String(10), nullable=False)
    object_key = Column(String(1024), nullable=False)
    local_sha1 = Column(String(40), nullable=True)
    remote_sha1 = Column(String(40), nullable=True)
    uploaded_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    refused_at = Column(DateTime, nullable=True)
    refusal = Column(String(20), nullable=True)
    last_skip_reason = Column(String(20), nullable=True)
    skip_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("object_key", name="colour_card_objects_object_key_key"),
        UniqueConstraint("shot_id", "kind", name="colour_card_objects_shot_id_kind_key"),
        CheckConstraint(OBJECT_KIND_CHECK, name="check_colour_card_object_kind"),
        CheckConstraint(OBJECT_STATE_CHECK, name="check_colour_card_object_state"),
        CheckConstraint(OBJECT_REFUSAL_CHECK, name="check_colour_card_object_refusal"),
    )


@event.listens_for(ColourCardObject, "before_update")
def _object_key_is_frozen(mapper, connection, target):
    from sqlalchemy import inspect

    history = inspect(target).attrs.object_key.history
    if history.deleted and history.deleted[0] != target.object_key:
        raise ImmutableFieldError(f"colour card object {target.id}: object_key is fixed at first upload")
