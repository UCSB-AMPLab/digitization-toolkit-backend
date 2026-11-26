"""Colour card shots on disk (NEH-340): the volume's `calibration/` folder, its manifest lines, integrity and rebuilds.

- Each shot writes a `calibration` line (capture.manifestHandler.CALIBRATION_RECORD_TYPE),
  one of the non-capture record types: no captured id, no bag line, no
  rebuilt record comes from it.
- Integrity counts a shot's file and its preview as known through the shot's
  row, never as orphans, and reports a shot whose file is missing.
- The layout migration owns a volume's `calibration/` files that its shots name.

    python -m pytest tests/unit/test_colour_card_disk.py
"""

import json

import pytest

from tests.unit.test_colour_card import _complete, _ok, _open, _shot, _take_set, _volume, card  # noqa: F401
from tests.unit.test_testing_space_capture import backend, lookups, unit  # noqa: F401  (fixtures)


def _integrity(db):
    from app.core.integrity import run_integrity_check
    db.expire_all()
    return run_integrity_check(db, verify_hashes=True)


@pytest.mark.unit
def test_a_shot_is_a_non_capture_manifest_line():
    from capture.manifestHandler import CALIBRATION_RECORD_TYPE, NON_CAPTURE_RECORD_TYPES, is_capture_record
    assert CALIBRATION_RECORD_TYPE == "calibration"
    assert CALIBRATION_RECORD_TYPE in NON_CAPTURE_RECORD_TYPES
    assert not is_capture_record({"record_type": "calibration", "capture_id": "x"})


@pytest.mark.unit
def test_integrity_knows_the_shots_and_their_previews(card):
    card.backend.raw = True
    v = _volume(card)
    _take_set(card, v.id)
    report = _integrity(card.db)
    folder = str((card.root / v.filename_stem / "calibration").resolve())
    assert [p for p in report["orphan_files"] if p.startswith(folder)] == []
    assert report["missing_colour_card_files"] == []
    assert report["summary"]["colour_card_shots"] == 2


@pytest.mark.unit
def test_integrity_reports_a_shot_whose_file_is_missing(card):
    from app.models.colour_card import ColourCardShot
    v = _volume(card)
    _take_set(card, v.id)
    shot = card.db.query(ColourCardShot).order_by(ColourCardShot.camera_index).first()
    (card.root / v.filename_stem / "calibration" / shot.filename).unlink()
    report = _integrity(card.db)
    assert [m["colour_card_shot_id"] for m in report["missing_colour_card_files"]] == [shot.id]
    assert report["summary"]["missing_colour_card_files"] == 1
    assert report["summary"]["ok"] is False


@pytest.mark.unit
def test_a_shot_file_no_row_names_is_an_orphan(card):
    from app.models.colour_card import ColourCardSet
    v = _volume(card)
    _take_set(card, v.id)
    # The rows go, the files stay: the manifest line alone does not make them known.
    for s in card.db.query(ColourCardSet).all():
        card.db.delete(s)
    card.db.commit()
    report = _integrity(card.db)
    folder = str((card.root / v.filename_stem / "calibration").resolve())
    assert len([p for p in report["orphan_files"] if p.startswith(folder)]) == 2


@pytest.mark.unit
def test_shots_take_no_place_among_the_captured_ids_and_the_bag_lines(card):
    from app.core import config
    from app.core.integrity import build_fixity_index, capture_record_lines
    from app.models.colour_card import ColourCardShot
    v = _volume(card)
    _take_set(card, v.id)
    ids = {s.capture_id for s in card.db.query(ColourCardShot).all()}
    index = build_fixity_index(config.settings.projects_dir.resolve())
    assert not ids & index.captured
    assert capture_record_lines(config.settings.projects_dir.resolve(), ids) == []


@pytest.mark.unit
def test_a_rebuild_from_disk_reads_no_shot_as_a_capture(card):
    from app.core.rebuild_volume import _manifest_captures
    v = _volume(card)
    _take_set(card, v.id)
    lines = [json.loads(line) for line in
             (card.root / v.filename_stem / "metadata" / "manifest.jsonl").read_text().splitlines()]
    assert sum(1 for line in lines if line.get("record_type") == "calibration") == 2
    assert _manifest_captures(card.root / v.filename_stem) == {}


@pytest.mark.unit
def test_the_layout_migration_owns_the_volumes_shots(card):
    from app.core.volume_layout import _foreign_entries
    v = _volume(card)
    card.backend.raw = True
    _take_set(card, v.id)
    assert _foreign_entries(card.db, card.root / v.filename_stem, v.id) == []
    (card.root / v.filename_stem / "calibration" / "suelto.jpg").write_bytes(b"x")
    assert _foreign_entries(card.db, card.root / v.filename_stem, v.id) == ["calibration/suelto.jpg"]


@pytest.mark.unit
def test_integrity_reports_a_shot_whose_bytes_changed(card):
    from app.models.colour_card import ColourCardShot
    v = _volume(card)
    _take_set(card, v.id)
    shot = card.db.query(ColourCardShot).order_by(ColourCardShot.camera_index).first()
    (card.root / v.filename_stem / "calibration" / shot.filename).write_bytes(b"other bytes")
    report = _integrity(card.db)
    assert [(m["colour_card_shot_id"], m["kind"]) for m in report["colour_card_mismatches"]] == [(shot.id, "shot")]
    assert report["summary"]["ok"] is False


@pytest.mark.unit
def test_integrity_reports_a_shot_whose_preview_alone_is_missing(card):
    from app.models.colour_card import ColourCardShot
    card.backend.raw = True
    v = _volume(card)
    _take_set(card, v.id)
    shot = card.db.query(ColourCardShot).order_by(ColourCardShot.camera_index).first()
    (card.root / v.filename_stem / "calibration" / shot.preview_filename).unlink()
    report = _integrity(card.db)
    assert [(m["colour_card_shot_id"], m["kind"]) for m in report["missing_colour_card_files"]] == [
        (shot.id, "preview")]
    assert report["summary"]["ok"] is False


@pytest.mark.unit
def test_integrity_verifies_a_shots_preview_digest(card):
    from app.models.colour_card import ColourCardShot
    card.backend.raw = True
    v = _volume(card)
    _take_set(card, v.id)
    shot = card.db.query(ColourCardShot).order_by(ColourCardShot.camera_index).first()
    (card.root / v.filename_stem / "calibration" / shot.preview_filename).write_bytes(b"other")
    report = _integrity(card.db)
    assert [(m["colour_card_shot_id"], m["kind"]) for m in report["colour_card_mismatches"]] == [
        (shot.id, "preview")]


def _counted_hashes(monkeypatch):
    from app.core import integrity
    hashed = []
    real = integrity._sha256

    def counting(path):
        hashed.append(str(path))
        return real(path)
    monkeypatch.setattr(integrity, "_sha256", counting)
    return hashed


@pytest.mark.unit
def test_a_bounded_check_hashes_at_most_its_budget_shots_included(card, monkeypatch):
    from app.core.integrity import run_integrity_check
    card.backend.raw = True
    for n in range(3):
        _take_set(card, _volume(card, f"xx-card-b{n}", f"Tomo {n}").id)
    hashed = _counted_hashes(monkeypatch)
    card.db.expire_all()
    report = run_integrity_check(card.db, verify_hashes=True, max_hash_checks=1)
    assert len(hashed) == 1, hashed
    assert report["summary"]["hashes_checked"] == 1
    # The unit's page images (two) and 3 sets x 2 shots x (master + preview).
    assert report["summary"]["hashes_eligible"] == 2 + 12
    assert report["summary"]["hash_sampled"] is True


@pytest.mark.unit
def test_a_full_check_hashes_every_shot_and_preview(card, monkeypatch):
    from app.core.integrity import run_integrity_check
    card.backend.raw = True
    _take_set(card, _volume(card).id)
    hashed = _counted_hashes(monkeypatch)
    card.db.expire_all()
    report = run_integrity_check(card.db, verify_hashes=True, max_hash_checks=None)
    calibration = [h for h in hashed if "/calibration/" in h]
    assert len(calibration) == 4 and report["summary"]["hashes_checked"] == len(hashed) == 2 + 4
    assert report["summary"]["hash_sampled"] is False
