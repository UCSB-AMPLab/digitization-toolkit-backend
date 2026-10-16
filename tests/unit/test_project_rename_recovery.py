#!/usr/bin/env python3
"""Crash-safe recovery for an interrupted project rename.

Power loss is this appliance's defining failure mode, so a rename that moves the
on-disk tree and then commits new paths must survive a crash between the two
steps without leaving records dangling. reconcile_pending_rename replays or
discards the intent journal at startup; these tests cover its three branches.
"""
import pytest

import app.core.storage_ops as so
from app.models.project import Project
from app.models.record import Record, RecordImage


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the journal at a writable temp data dir (default /var/lib/dtk is not writable in tests)."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "DTK_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _add_project(db, name):
    p = Project(name=name)
    db.add(p)
    db.commit()
    return p


def _add_image(db, record_id, path):
    img = RecordImage(
        record_id=record_id,
        filename=path.name,
        file_path=str(path),
        thumbnail_path=str(path.with_suffix(".thumb.jpg")),
        capture_id="c1",
        format="jpg",
        file_size=1,
    )
    db.add(img)
    db.commit()
    return img


@pytest.mark.unit
def test_reconcile_finishes_db_when_files_already_moved(db_session, data_dir):
    """Crash after the FS move but before the commit: DB still old, files at new_root -> finish the DB side."""
    root = data_dir / "projects"
    old_root, new_root = root / "old", root / "new"
    new_root.mkdir(parents=True)
    (new_root / "img.jpg").write_text("x")

    p = _add_project(db_session, "old")
    rec = Record(title="r", project_id=p.id, status="in_review", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    _add_image(db_session, rec.id, old_root / "img.jpg")

    so.write_rename_journal(p.id, "old", "new", old_root, new_root)
    so.reconcile_pending_rename(db_session)

    db_session.refresh(p)
    img = db_session.query(RecordImage).first()
    assert p.name == "new"
    assert img.file_path == str(new_root / "img.jpg")
    assert so.read_rename_journal() is None


@pytest.mark.unit
def test_reconcile_aborts_when_move_never_happened(db_session, data_dir):
    """Crash before the FS move: DB and disk are both still old -> discard the journal, change nothing."""
    root = data_dir / "projects"
    old_root, new_root = root / "p", root / "p_ren"
    old_root.mkdir(parents=True)

    p = _add_project(db_session, "p")
    so.write_rename_journal(p.id, "p", "p_ren", old_root, new_root)
    so.reconcile_pending_rename(db_session)

    db_session.refresh(p)
    assert p.name == "p"
    assert so.read_rename_journal() is None


@pytest.mark.unit
def test_reconcile_completes_move_when_db_already_renamed(db_session, data_dir):
    """Crash after the commit but before clearing the journal: DB new, files still old -> finish the FS move."""
    root = data_dir / "projects"
    old_root, new_root = root / "old2", root / "new2"
    old_root.mkdir(parents=True)
    (old_root / "f").write_text("y")

    p = _add_project(db_session, "new2")
    so.write_rename_journal(p.id, "old2", "new2", old_root, new_root)
    so.reconcile_pending_rename(db_session)

    assert new_root.exists() and not old_root.exists()
    assert so.read_rename_journal() is None
