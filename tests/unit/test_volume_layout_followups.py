"""Follow-ups to the volume layout migration: old journals, pending sidecars, adoption, fixity after renames.

Uses the fixtures of test_volume_dir_by_id: the name-based `legacy` layout
under the per-test projects root, and a writable data directory.
"""

import json
import os
from pathlib import Path

import pytest

from tests.unit.test_volume_dir_by_id import capture_settings, data_dir, legacy  # noqa: F401


# ---------------------------------------------------------------------------
# A journal written by the first version of the migration
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_v1_journal_left_by_the_earlier_commits_is_completed(db_session, legacy, data_dir):
    """2d1116b wrote version-1 entries, with no "adopt" and no "capture_id". The crash came after the first move."""
    from app.core.volume_layout import JOURNAL_NAME, migrate_volume_layout_at_startup
    from app.models.record import RecordImage

    i2 = legacy["i2"]
    src = Path(i2.file_path).resolve()
    dst = (legacy["root"] / "proj" / f"collection-{legacy['a2'].id}" / "images" / "main" / src.name).resolve()
    entry = {"image_id": i2.id, "record_id": i2.record_id, "collection_id": legacy["a2"].id,
             "file_path": i2.file_path, "filename": i2.filename, "from": str(src), "to": str(dst),
             "sidecar_from": None, "sidecar_to": None, "thumb_from": None, "thumb_to": None, "thumb_clear": None}
    (data_dir / JOURNAL_NAME).write_text(json.dumps({"version": 1, "root": str(legacy["root"].resolve()),
                                                     "collection_id": legacy["a2"].id, "entries": [entry]}))
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)  # the move happened, the commit did not

    migrate_volume_layout_at_startup(db_session)

    db_session.expire_all()
    row = db_session.get(RecordImage, i2.id)
    assert Path(row.file_path) == dst
    assert dst.read_bytes() == b"bytes of IMG_0002.jpg"
    assert not (data_dir / JOURNAL_NAME).exists()
