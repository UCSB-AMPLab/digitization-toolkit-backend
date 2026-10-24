"""A torn append to a capture manifest never swallows the next record.

Every append (a capture record, a relocation record, the migration's copy of
a volume's records) first makes sure the manifest ends with a newline,
writing one if a power cut left the last line torn, so the torn line stays a
separate line, which the index skips and logs. A copy torn that way is
appended again by the next run, so every record ends up present and parseable
once. The copy is not written whole and renamed: on exFAT or vfat a rename
over an existing file is not shown to be crash-atomic, and the manifest may
be a volume's only one.

The torn-write tests are the verifier's (round 4), the second adjusted in
round 5 to the append.
"""

import json
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import STEMS, _capture_records, capture_settings, data_dir, legacy  # noqa: F401


class _Crash(BaseException):
    pass


@pytest.mark.unit
def test_torn_copy_probe(db_session, legacy, monkeypatch):
    import capture.manifestHandler as mh
    import app.core.volume_layout as vl
    from app.core.integrity import build_fixity_index
    from capture.manifestHandler import append_manifest_record

    _capture_records(db_session, legacy)
    real_write = mh.os.write
    hits = []

    def torn(fd, data):
        hits.append(len(data))
        real_write(fd, bytes(data)[: len(data) // 2])
        raise _Crash()

    monkeypatch.setattr(mh.os, "write", torn)
    with pytest.raises(_Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(mh.os, "write", real_write)
    root = legacy["root"]
    for m in sorted(root.glob("*/metadata/manifest.jsonl")):
        print("\nAFTER CRASH", m.relative_to(root), repr(m.read_text()[-80:]))
    vl.migrate_volume_layout(db_session)
    for m in sorted(root.glob("*/metadata/manifest.jsonl")):
        lines = m.read_text().splitlines()
        bad = [l[:60] for l in lines if l.strip() and not _ok(l)]
        print("AFTER RERUN", m.relative_to(root), len(lines), "lines; unparseable:", bad)
    vol = root / STEMS["a1"]

    class Rec:
        capture_id = "cap-after"

        def to_dict(self):
            return {"capture_id": "cap-after", "files": [{"relative_path": "captures/AFTER.jpg", "sha256": "0" * 64}]}

    append_manifest_record(vol, Rec(), "capture")
    idx = build_fixity_index(root.resolve())
    print("HITS", hits, "cap-after indexed:", "cap-after" in idx.captured, "cap-i1 indexed:", "cap-i1" in idx.captured)
    assert "cap-after" in idx.captured


def _ok(l):
    try:
        json.loads(l)
        return True
    except ValueError:
        return False


@pytest.mark.unit
def test_a_torn_copy_is_appended_again_and_its_junk_line_logged(db_session, legacy, monkeypatch, caplog):
    import capture.manifestHandler as mh
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)
    real_write = mh.os.write

    def torn(fd, data):
        real_write(fd, bytes(data)[: len(data) // 2])
        raise _Crash()

    monkeypatch.setattr(mh.os, "write", torn)
    with pytest.raises(_Crash):
        vl.migrate_volume_layout(db_session)
    db_session.rollback()
    monkeypatch.setattr(mh.os, "write", real_write)
    vl.migrate_volume_layout(db_session)
    vl.migrate_volume_layout(db_session)
    manifest = legacy["root"] / STEMS["a1"] / "metadata" / "manifest.jsonl"
    lines = manifest.read_text().splitlines()
    parsed = [json.loads(l) for l in lines if _ok(l)]
    # Every record present and parseable once; the torn line stays apart, as junk.
    assert [p["capture_id"] for p in parsed] == ["cap-i1"]
    assert len([l for l in lines if l.strip() and not _ok(l)]) == 1, lines
    caplog.clear()
    with caplog.at_level("WARNING", logger="app.core.integrity"):
        from app.core.integrity import build_fixity_index
        assert "cap-i1" in build_fixity_index(legacy["root"].resolve()).captured
    assert str(manifest) in caplog.text and "1 line" in caplog.text



class _Rec:
    def __init__(self, cid):
        self.capture_id = cid

    def to_dict(self):
        return {"capture_id": self.capture_id, "files": [{"relative_path": f"captures/{self.capture_id}.jpg",
                                                         "sha256": "0" * 64}]}


@pytest.mark.unit
def test_the_capture_after_a_torn_record_keeps_its_fixity(override_projects_root, capture_settings, data_dir, monkeypatch):
    import capture.manifestHandler as mh
    from app.core.integrity import build_fixity_index
    from capture.manifestHandler import append_manifest_record
    from capture.project_manager import ensure_volume_dirs

    vol = ensure_volume_dirs("xx-test-torn")
    append_manifest_record(vol, _Rec("cap-1"), "capture")
    real = mh.os.write

    def torn(fd, data):
        real(fd, bytes(data)[: len(data) // 2])
        raise _Crash()

    monkeypatch.setattr(mh.os, "write", torn)
    with pytest.raises(_Crash):
        append_manifest_record(vol, _Rec("cap-2"), "capture")
    monkeypatch.setattr(mh.os, "write", real)
    append_manifest_record(vol, _Rec("cap-3"), "capture")

    idx = build_fixity_index(override_projects_root.resolve())
    print("\nINDEXED", sorted(idx.captured))
    assert "cap-3" in idx.captured


@pytest.mark.unit
def test_the_index_skips_and_logs_a_line_it_cannot_parse(override_projects_root, capture_settings, caplog):
    from app.core.integrity import build_fixity_index

    m = override_projects_root / "xx-test-log" / "metadata" / "manifest.jsonl"
    m.parent.mkdir(parents=True)
    m.write_text('{"capture_id": "cap-1", "files": []}\n{"capture_id": "cap-2", "fi\n{"capture_id": "cap-3", "files": []}\n')

    with caplog.at_level("WARNING", logger="app.core.integrity"):
        index = build_fixity_index(override_projects_root.resolve())

    assert index.captured == {"cap-1", "cap-3"}
    assert str(m) in caplog.text and "1 line" in caplog.text


@pytest.mark.unit
def test_a_relocation_record_after_a_torn_line_is_read(override_projects_root, capture_settings):
    from app.core.integrity import build_fixity_index
    from capture.manifestHandler import append_relocation_record

    vol = override_projects_root / "xx-test-rel"
    (vol / "metadata").mkdir(parents=True)
    (vol / "metadata" / "manifest.jsonl").write_text('{"capture_id": "cap-1", "fi')

    append_relocation_record(vol, "cap-1", "captures/a.jpg", "captures/b.jpg", image_id=7, sha256="ab" * 32)

    assert build_fixity_index(override_projects_root.resolve()).by_image[("cap-1", 7)] == "ab" * 32


@pytest.mark.unit
def test_the_copy_appends_after_a_torn_last_line(db_session, legacy):
    """The copy goes through the same append as a capture: a newline first, if the last line is torn."""
    from app.core.volume_layout import migrate_volume_layout

    _capture_records(db_session, legacy)
    meta = legacy["root"] / STEMS["a1"] / "metadata"
    meta.mkdir(parents=True)
    (meta / "manifest.jsonl").write_text('{"capture_id": "cap-i1", "fi')  # a capture record a power cut tore

    migrate_volume_layout(db_session)

    lines = (meta / "manifest.jsonl").read_text().splitlines()
    assert lines[0] == '{"capture_id": "cap-i1", "fi'
    assert [json.loads(l)["capture_id"] for l in lines[1:]] == ["cap-i1"]


@pytest.mark.unit
def test_the_copy_leaves_no_part_file_and_a_stale_one_is_not_foreign(db_session, legacy):
    from app.core.volume_layout import migrate_volume_layout

    _capture_records(db_session, legacy)
    meta = legacy["root"] / STEMS["a1"] / "metadata"
    meta.mkdir(parents=True)
    (meta / "manifest.jsonl.part").write_text('{"capture_id": "cap-i1", "fi')  # a crashed copy's

    report = migrate_volume_layout(db_session)

    assert not [s for s in report["skipped"] if s["reason"] == "stem_dir_occupied"]
    assert not (meta / "manifest.jsonl.part").exists()
    assert [json.loads(l)["capture_id"] for l in (meta / "manifest.jsonl").read_text().splitlines()] == ["cap-i1"]
