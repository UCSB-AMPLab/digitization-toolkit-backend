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


@pytest.mark.unit
@pytest.mark.parametrize("rel", [
    "captures/IMG_0099.part.cr2",           # atomic_write of IMG_0099.cr2
    "captures/IMG_0099.part",               # atomic_write of a name with no suffix
    f"captures/.renumber_{HEX}.jpg",        # renumber, phase 1
    "previews/IMG_0099_preview.part.jpg",   # atomic_write of a RAW preview
    f"previews/.renumber_{HEX}.jpg",
    "captures/.IMG_0099.jpg.part",          # the migration's cross-filesystem move
    "thumbnails/.IMG_0099_thumb.jpg.part",
])
def test_a_writers_temp_file_is_listed_left_alone_and_the_volume_migrates(db_session, legacy, rel):
    from app.core.volume_layout import migrate_volume_layout

    temp = legacy["root"] / STEMS["a2"] / rel
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_bytes(b"half a file")

    report = migrate_volume_layout(db_session)

    assert [m for m in report["moved"] if m["collection_id"] == legacy["a2"].id]
    assert not [s for s in report["skipped"] if s["reason"] == "stem_dir_occupied"]
    assert report["temp_files_left"] == [str(temp.resolve())]
    assert temp.read_bytes() == b"half a file"


@pytest.mark.unit
@pytest.mark.parametrize("rel", [
    "captures/IMG_0099.partial.jpg",
    "captures/IMG_0099.jpg.part",           # not atomic_write's (stem.part.suffix) nor _move's (leading dot)
    "captures/.renumber_xyz.jpg",
    f"captures/.renumber_{HEX[:31]}.jpg",
    "thumbnails/IMG_0099_thumb.part.jpg",   # atomic_write never writes thumbnails
    f"thumbnails/.renumber_{HEX}.jpg",      # nor does a renumber
])
def test_a_name_close_to_a_temp_name_is_still_foreign(db_session, legacy, rel):
    from app.core.volume_layout import migrate_volume_layout

    f = legacy["root"] / STEMS["a2"] / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")

    report = migrate_volume_layout(db_session)

    assert not [m for m in report["moved"] if m["collection_id"] == legacy["a2"].id]
    assert {tuple(s["foreign"]) for s in report["skipped"] if s["reason"] == "stem_dir_occupied"} == {(rel,)}
    assert report["temp_files_left"] == []


# ---------------------------------------------------------------------------
# Thumbnails no row owns
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_unowned_thumbnail_named_as_the_generator_names_them_is_listed_and_the_volume_migrates(db_session, legacy):
    """app.core.thumbnail.generate_thumbnail names its files `<uuid4 hex>_thumb.jpg`; one is regenerable."""
    from app.core.volume_layout import migrate_volume_layout

    thumb = legacy["root"] / STEMS["a2"] / "thumbnails" / f"{HEX}_thumb.jpg"
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(b"a thumbnail whose row was never committed")

    report = migrate_volume_layout(db_session)

    assert [m for m in report["moved"] if m["collection_id"] == legacy["a2"].id]
    assert not [s for s in report["skipped"] if s["reason"] == "stem_dir_occupied"]
    assert report["orphan_thumbnails_left"] == [str(thumb.resolve())]
    assert thumb.read_bytes() == b"a thumbnail whose row was never committed"


@pytest.mark.unit
@pytest.mark.parametrize("rel", [
    f"thumbnails/{HEX[:31]}_thumb.jpg",
    f"thumbnails/{HEX.upper()}_thumb.jpg",
    f"thumbnails/{HEX}_thumb.png",
    f"captures/{HEX}_thumb.jpg",            # the generator writes only into thumbnails/
])
def test_a_name_close_to_the_generators_is_still_foreign(db_session, legacy, rel):
    from app.core.volume_layout import migrate_volume_layout

    f = legacy["root"] / STEMS["a2"] / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")

    report = migrate_volume_layout(db_session)

    assert {tuple(s["foreign"]) for s in report["skipped"] if s["reason"] == "stem_dir_occupied"} == {(rel,)}
    assert report["orphan_thumbnails_left"] == []
