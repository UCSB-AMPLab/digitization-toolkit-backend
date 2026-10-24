"""At every startup, a migrated volume's own manifest is checked, and any capture record it lacks is copied in.

For each volume with a stem whose images are all in place, its own
`metadata/manifest.jsonl` must hold a capture record that can be parsed for
each capture id of its images. A missing one (a copy that failed, or was
torn) is copied from the other manifests as the migration copies, so the next
start repairs it. Volumes still in the old layout are left to the migration.
"""

import json
import time
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _capture_records, capture_settings, data_dir, legacy,
)


def _ids(path: Path) -> list:
    out = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("record_type") != "relocation":
            out.append(rec["capture_id"])
    return sorted(out)


def _own(L, key) -> Path:
    return L["root"] / STEMS[key] / "metadata" / "manifest.jsonl"


@pytest.mark.unit
def test_a_lost_copy_is_repaired_at_the_next_startup(db_session, legacy):
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)
    vl.migrate_volume_layout_at_startup(db_session)
    _own(legacy, "a2").unlink()

    report = vl.migrate_volume_layout(db_session)

    assert _ids(_own(legacy, "a2")) == ["cap-i2", "cap-i3a", "cap-i3b"]
    assert report["manifests_repaired"] == {legacy["a2"].id: 3}


@pytest.mark.unit
def test_a_torn_copy_is_repaired_and_the_torn_line_left_apart(db_session, legacy):
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)
    vl.migrate_volume_layout_at_startup(db_session)
    own = _own(legacy, "a1")
    own.write_text(own.read_text()[:30])  # a power cut through the only line

    vl.migrate_volume_layout_at_startup(db_session)

    assert _ids(own) == ["cap-i1"]
    assert len(own.read_text().splitlines()) == 2


@pytest.mark.unit
def test_a_whole_manifest_is_not_written_again(db_session, legacy):
    import app.core.volume_layout as vl

    _capture_records(db_session, legacy)
    vl.migrate_volume_layout_at_startup(db_session)
    before = {k: _own(legacy, k).read_bytes() for k in ("a1", "a2", "tomo")}

    report = vl.migrate_volume_layout(db_session)

    assert {k: _own(legacy, k).read_bytes() for k in ("a1", "a2", "tomo")} == before
    assert report["manifests_repaired"] == {}


@pytest.mark.unit
def test_a_volume_still_in_the_old_layout_is_not_repaired(db_session, legacy):
    """Its records are copied when the migration moves it, not before."""
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    stray = legacy["root"] / STEMS["a1"] / "notes.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("someone else's")  # a1 is skipped as stem_dir_occupied

    report = vl.migrate_volume_layout(db_session)

    assert not _own(legacy, "a1").exists()
    assert legacy["a1"].id not in report["manifests_repaired"]
    assert db_session.get(RecordImage, legacy["i1"].id).file_path.endswith("fondo_a/images/main/IMG_0001.CR2")


@pytest.mark.unit
def test_a_capture_id_no_manifest_holds_is_reported_and_writes_nothing(db_session, legacy, caplog):
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    vl.migrate_volume_layout_at_startup(db_session)
    img = db_session.get(RecordImage, legacy["i2"].id)
    img.capture_id = "cap-nowhere"
    db_session.commit()
    before = _own(legacy, "a2").read_bytes()

    with caplog.at_level("WARNING", logger="app.core.volume_layout"):
        report = vl.migrate_volume_layout(db_session)

    assert _own(legacy, "a2").read_bytes() == before
    assert report["captures_without_record"] == {legacy["a2"].id: ["cap-nowhere"]}
    assert "cap-nowhere" in caplog.text


@pytest.mark.unit
def test_the_check_on_a_rionegro_sized_unit(db_session, capture_settings, data_dir, override_projects_root, capsys):
    """25 volumes, 6,700 images, every record in place: one query, 25 manifests read, nothing written."""
    from sqlalchemy import event
    import app.core.volume_layout as vl
    import app.core.integrity as integrity
    from app.models.collection import Collection
    from app.models.project import Project
    from app.models.record import Record, RecordImage

    p = Project(name="Rionegro")
    db_session.add(p)
    db_session.commit()
    per = 268
    for v in range(25):
        stem = f"xx-test-rio{v:02d}"
        c = Collection(name=f"Tomo {v}", project_id=p.id, filename_stem=stem)
        db_session.add(c)
        db_session.commit()
        cap = override_projects_root / stem / "captures"
        cap.mkdir(parents=True)
        (cap.parent / "metadata").mkdir()
        lines, imgs = [], []
        recs = [Record(title=f"r{n}", collection_id=c.id, status="captured", capture_mode="single")
                for n in range(per)]
        db_session.add_all(recs)
        db_session.commit()
        for n, r in enumerate(recs):
            f = cap / f"{stem}-img_{n:04d}_c0.jpg"
            f.write_bytes(b"x")
            cid = f"cap-{v}-{n}"
            lines.append(json.dumps({"capture_id": cid, "files": [
                {"relative_path": f"captures/{f.name}", "sha256": "0" * 64}]}))
            imgs.append(RecordImage(record_id=r.id, filename=f.name, file_path=str(f), format="jpg",
                                    role="single", capture_id=cid))
        db_session.add_all(imgs)
        db_session.commit()
        (cap.parent / "metadata" / "manifest.jsonl").write_text("\n".join(lines) + "\n")
    db_session.expire_all()
    stmts = []
    eng = db_session.get_bind()
    listener = lambda conn, cur, stmt, params, ctx, many: stmts.append(stmt.split()[0])
    reads = []
    real_manifests = integrity._manifests
    integrity_calls = lambda root: (reads.append(1), real_manifests(root))[1]
    event.listen(eng, "before_cursor_execute", listener)
    vl_manifests = integrity._manifests
    integrity._manifests = integrity_calls
    try:
        t0 = time.monotonic()
        report = vl.migrate_volume_layout(db_session)
        elapsed = time.monotonic() - t0
    finally:
        integrity._manifests = vl_manifests
        event.remove(eng, "before_cursor_execute", listener)

    with capsys.disabled():
        print(f"\nRIONEGRO-SIZED CHECK: {elapsed:.3f}s, {len(stmts)} queries, other manifests scanned: {len(reads)}")
    assert report["manifests_repaired"] == {}
    assert reads == []  # nothing missing, so no manifest but the volumes' own is read
    assert elapsed < 2


# ---------------------------------------------------------------------------
# A capture id no manifest holds: one full pass, then only while something changes
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_capture_id_no_manifest_holds_does_not_rescan_every_manifest_at_every_start(db_session, legacy, monkeypatch):
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    vl.migrate_volume_layout(db_session)
    img = db_session.get(RecordImage, legacy["i2"].id)
    img.capture_id = "client-sent-id"  # an upload whose client sent a capture id
    db_session.commit()
    calls = {"n": 0}
    real = vl._records_to_copy

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(vl, "_records_to_copy", counted)
    reports = [vl.migrate_volume_layout(db_session) for _ in range(3)]
    assert calls["n"] <= 1
    assert all(r["captures_without_record"] == {legacy["a2"].id: ["client-sent-id"]} for r in reports)


@pytest.mark.unit
def test_a_manifest_that_changes_brings_the_full_pass_back(db_session, legacy, monkeypatch):
    """Once a record for the missing id is written somewhere, the next start finds and copies it."""
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    vl.migrate_volume_layout(db_session)
    img = db_session.get(RecordImage, legacy["i2"].id)
    img.capture_id = "cap-late"
    db_session.commit()
    assert vl.migrate_volume_layout(db_session)["captures_without_record"] == {legacy["a2"].id: ["cap-late"]}
    with open(legacy["root"] / "proj" / "metadata" / "manifest.jsonl", "a") as m:
        m.write(json.dumps({"capture_id": "cap-late", "files": [
            {"relative_path": "fondo_a/images/main/IMG_0002.jpg", "sha256": "0" * 64}]}) + "\n")

    report = vl.migrate_volume_layout(db_session)

    assert report["captures_without_record"] == {}
    assert "cap-late" in _ids(_own(legacy, "a2"))


@pytest.mark.unit
def test_the_memo_lives_in_the_data_directory_and_goes_when_nothing_is_missing(db_session, legacy, data_dir):
    import app.core.volume_layout as vl
    from app.models.record import RecordImage

    _capture_records(db_session, legacy)
    vl.migrate_volume_layout(db_session)
    img = db_session.get(RecordImage, legacy["i2"].id)
    real = img.capture_id
    img.capture_id = "cap-nowhere"
    db_session.commit()
    vl.migrate_volume_layout(db_session)
    memo = data_dir / vl.MISSING_RECORDS_NAME
    assert json.loads(memo.read_text())["missing"] == {str(legacy["a2"].id): ["cap-nowhere"]}
    assert not any(legacy["root"].rglob(vl.MISSING_RECORDS_NAME))

    img.capture_id = real
    db_session.commit()
    vl.migrate_volume_layout(db_session)
    assert not memo.exists()
