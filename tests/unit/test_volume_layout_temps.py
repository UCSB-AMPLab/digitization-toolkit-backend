"""Leftovers of writers interrupted by a power cut, in a volume's `<stem>/`.

- A `metadata/manifest.jsonl.part` (append_manifest_lines) is removed at the
  start of every migration run and counted as `stale_parts_removed`; nothing
  else writes the manifest at startup.
- In `captures/`, `previews/` and `thumbnails/`, a file named exactly as one
  of the writers into them names its temporary file is not foreign: the
  migration neither moves nor deletes it, lists it under `temp_files_left`,
  and goes on with the volume. The names are those of capture.utils.atomic_write
  (`<stem>.part<suffix>`, captures and previews), renumber_collection_images
  (`.renumber_<32 hex><suffix>`, captures and previews) and the migration's own
  cross-filesystem _move (`.<name>.part`, all three). Any other file no row
  owns is foreign and the volume is skipped.
"""

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _capture_records, capture_settings, data_dir, legacy,
)

HEX = "0123456789abcdef" * 2


@pytest.mark.unit
def test_a_stale_part_is_removed_at_the_start_of_the_run(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    _capture_records(db_session, legacy)
    migrate_volume_layout(db_session)
    part = legacy["root"] / STEMS["a1"] / "metadata" / "manifest.jsonl.part"
    part.write_text('{"capture_id": "cap-i1", "fi')
    manifest = (legacy["root"] / STEMS["a1"] / "metadata" / "manifest.jsonl").read_bytes()

    dry = migrate_volume_layout(db_session, dry_run=True)
    assert dry["stale_parts_removed"] == 1 and part.exists()

    report = migrate_volume_layout(db_session)

    assert report["stale_parts_removed"] == 1
    assert not part.exists()
    assert (legacy["root"] / STEMS["a1"] / "metadata" / "manifest.jsonl").read_bytes() == manifest
    assert migrate_volume_layout(db_session)["stale_parts_removed"] == 0
