"""The offsite upload hold: a file whose presence stops everything sent offsite.

While a unit is upgraded, provisioning writes the tenant to offsite.env
before the one-time commands run (adopt-map, adopt, raise-capture-counter,
mark-freed, backfill-preview-digests). The running backend read its settings
at start and uploads nothing, but a crash, a reboot or a power cut restarts
it with the tenant, and DTK_OFFSITE_BACKUP_DELAY later it would upload the
images not yet adopted a second time, under their disk names, with a key that
cannot delete them. The upgrade creates the hold file first and removes it
once those commands have run.

While the file exists:

* the image upload uploads nothing and lists nothing
  (image_upload.run_image_upload answers "held"); a run already draining
  when the file appears stops after the object in progress;
* the catalogue dump is not taken (catalogue_backup.run_catalogue_backup
  answers "held"); its sweep of stale dumps still runs;
* freeing is refused (offsite_free._prepare_free), since a volume can read
  as received while the commands that complete its objects' state are still
  being run. The dry run and restore are not held: neither writes to the
  bucket, and restore is the way back for a freed file.

Anything else that sends to the bucket checks `held` at the start of each of
its runs.

The hold is read at the start of every run, not when a schedule starts. The
schedulers start whether or not the file exists, so creating it takes effect
at the next tick and removing it resumes the next tick, with no restart. The
one-time commands do not read it: they are what runs during it.

The path is DTK_OFFSITE_HOLD_FILE, by default `offsite-hold` in DTK_DATA_DIR
(/var/lib/dtk/offsite-hold on a unit). Only the file's existence counts;
its contents are not read.
"""

import logging
from pathlib import Path

# As a module: tests rebuild the Settings object (see catalogue_backup).
from app.core import config

logger = logging.getLogger(__name__)


def hold_file() -> Path:
    """The hold file's path (DTK_OFFSITE_HOLD_FILE, else offsite-hold in the data dir)."""
    return config.settings.offsite_hold_file


def is_held() -> bool:
    """Whether the hold file exists now. Read afresh on every call."""
    return hold_file().exists()


def hold_state() -> dict:
    """`held` and `hold_file`, for the status routes."""
    path = hold_file()
    return {"held": path.exists(), "hold_file": str(path)}


def held(job: str) -> bool:
    """True, after one INFO line naming `job`, when the hold file exists.

    For the start of each run of anything that sends to the bucket: the
    image upload, the catalogue dump, and whatever is added after them.
    """
    path = hold_file()
    if not path.exists():
        return False
    logger.info("offsite uploads are on hold: %s exists; %s not run", path, job)
    return True
