"""A volume's `<stem>/`, or one of its four directories, that resolves outside the projects root is never moved into.

A symbolic link there (put by hand, or left by a drive mounted inside the
tree) would take the masters off the storage, where every reader then
refuses them (resolve_within_storage). The startup migration resolves each
destination and skips the volume with reason `stem_dir_outside_projects_root`.
"""

import pytest

from tests.unit.test_volume_layout_by_stem import STEMS, capture_settings, data_dir, legacy  # noqa: F401


@pytest.mark.unit
@pytest.mark.parametrize("link", ["", "captures", "previews", "thumbnails"])
def test_a_destination_linked_outside_the_root_is_not_moved_into(db_session, legacy, tmp_path, link):
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import RecordImage

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    vol = legacy["root"] / STEMS["a1"]
    if link:
        vol.mkdir()
        (vol / link).symlink_to(outside, target_is_directory=True)
    else:
        vol.symlink_to(outside, target_is_directory=True)
    before = db_session.get(RecordImage, legacy["i1"].id).file_path

    report = migrate_volume_layout(db_session)

    assert not [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]
    assert [s["reason"] for s in report["skipped"] if s["image_id"] == legacy["i1"].id] == [
        "stem_dir_outside_projects_root"]
    db_session.expire_all()
    assert db_session.get(RecordImage, legacy["i1"].id).file_path == before
    assert list(outside.rglob("*")) == []
    # The other volumes are moved as usual.
    assert [m for m in report["moved"] if m["collection_id"] == legacy["a2"].id]


@pytest.mark.unit
def test_a_link_that_stays_inside_the_root_is_followed(db_session, legacy):
    """A `<stem>/` linked to another directory under the projects root keeps the files on the storage."""
    from app.core.volume_layout import migrate_volume_layout

    inside = legacy["root"] / "kept-elsewhere-on-the-drive"
    inside.mkdir()
    (legacy["root"] / STEMS["a1"]).symlink_to(inside, target_is_directory=True)

    report = migrate_volume_layout(db_session)

    assert [m for m in report["moved"] if m["collection_id"] == legacy["a1"].id]
    assert (inside / "captures" / "IMG_0001.CR2").is_file()
