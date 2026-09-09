#!/usr/bin/env python3
"""The integrity check's hash verification must be bounded by default.

A full sweep re-hashes every stored image, which is I/O-punishing on a unit
holding tens of thousands of captures. run_integrity_check must hash only a
bounded (random) sample when max_hash_checks is set, and everything eligible
only when it is None (the explicit full-sweep opt-in).
"""
import hashlib
import json

import pytest

from app.core.integrity import run_integrity_check
from app.models.project import Project
from app.models.record import Record, RecordImage


def _seed_captures(db_session, projects_dir, n):
    proj_dir = projects_dir / "proj"
    (proj_dir / "images").mkdir(parents=True, exist_ok=True)
    (proj_dir / "metadata").mkdir(parents=True, exist_ok=True)

    project = Project(name="proj")
    db_session.add(project)
    db_session.commit()
    record = Record(title="r", project_id=project.id, status="in_review", capture_mode="single")
    db_session.add(record)
    db_session.commit()

    manifest_lines = []
    for i in range(n):
        rel = f"images/img_{i}.jpg"
        fpath = proj_dir / rel
        fpath.write_bytes(f"data-{i}".encode())
        sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
        manifest_lines.append(json.dumps({
            "capture_id": f"c{i}",
            "files": [{"sha256": sha, "relative_path": rel}],
        }))
        db_session.add(RecordImage(
            record_id=record.id,
            filename=f"img_{i}.jpg",
            file_path=str(fpath),
            capture_id=f"c{i}",
            format="jpg",
            file_size=1,
        ))
    (proj_dir / "metadata" / "manifest.jsonl").write_text("\n".join(manifest_lines), encoding="utf-8")
    db_session.commit()


@pytest.mark.unit
def test_hash_check_is_bounded_and_sampled(db_session, override_projects_root):
    _seed_captures(db_session, override_projects_root, n=8)

    report = run_integrity_check(db_session, verify_hashes=True, max_hash_checks=3)
    summary = report["summary"]

    assert summary["hashes_eligible"] == 8
    assert summary["hashes_checked"] == 3
    assert summary["hash_sampled"] is True


@pytest.mark.unit
def test_full_sweep_hashes_everything(db_session, override_projects_root):
    _seed_captures(db_session, override_projects_root, n=8)

    report = run_integrity_check(db_session, verify_hashes=True, max_hash_checks=None)
    summary = report["summary"]

    assert summary["hashes_eligible"] == 8
    assert summary["hashes_checked"] == 8
    assert summary["hash_sampled"] is False
