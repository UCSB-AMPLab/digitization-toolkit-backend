"""Database <-> filesystem <-> manifest reconciliation.

Field operations that are expected on this appliance (an SD re-clone, a
DB-snapshot restore) can leave the database and the image files silently
disagreeing, and nothing surfaces it until an export drops a page or a thumbnail
404s. This module compares, in both directions, the RecordImage rows against the
files on disk, spot-checks file bytes against the capture-time manifest hashes,
and reports parentless records. It is strictly read-only: it never moves,
deletes, or rewrites anything.
"""

import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core import config
from app.core.paths import RAW_SUFFIXES, is_raw_suffix, preview_sidecar_of, resolve_within_storage
from app.models.record import Record, RecordImage

logger = logging.getLogger(__name__)

# Original capture/upload formats. Thumbnails are derived artifacts and are excluded from the orphan scan by path (see _iter_image_files)
# RAW members come from RAW_SUFFIXES, the set the preview endpoint and the sidecar rule use.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"} | RAW_SUFFIXES
_SHA_CHUNK = 1024 * 1024


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_SHA_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_roots() -> List[Path]:
    """Directories walked to find image files on disk (captures and uploads)."""
    s = config.settings
    candidates = [s.projects_dir, s.data_dir / "uploads"]
    seen: set = set()
    out: List[Path] = []
    for r in candidates:
        try:
            rp = r.resolve()
        except OSError:
            continue
        if str(rp) not in seen and rp.exists():
            seen.add(str(rp))
            out.append(rp)
    return out


def _iter_image_files(root: Path):
    """Yield original image files under root, skipping derived thumbnails."""
    for p in root.rglob("*"):
        if "thumbnails" in p.parts:
            continue
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            yield p


def _build_manifest_index(projects_dir: Path) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    """Index every project's metadata/manifest.jsonl by capture_id.

    Returns (by_path, by_name): capture_id -> {absolute_path: sha256} and
    capture_id -> {filename: sha256}. The name map is a fallback for when a stored
    absolute path differs from the manifest's (e.g. after an SD re-clone remounts
    the tree at a different location).

    A relocation record (capture.manifestHandler.append_relocation_record)
    says a captured file was moved to another name: once every capture record
    is indexed, each relocation, in file order, gives its `to` path (and name)
    the sha256 its `from` path (or name) has, so a file renamed after capture
    is still checked against its capture-time hash.
    """
    by_path: Dict[str, Dict[str, str]] = {}
    by_name: Dict[str, Dict[str, str]] = {}
    relocations: List[Tuple[str, str, str]] = []  # (capture_id, from abs, to abs)
    if not projects_dir.exists():
        return by_path, by_name

    for manifest in projects_dir.glob("**/metadata/manifest.jsonl"):
        project_root = manifest.parent.parent
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = rec.get("capture_id")
            if not cid:
                continue
            if rec.get("record_type") == "relocation":
                frm, to = rec.get("from"), rec.get("to")
                if frm and to:
                    try:
                        relocations.append((cid, str((project_root / frm).resolve()), str((project_root / to).resolve())))
                    except OSError:
                        pass
                continue
            for f in rec.get("files", []):
                sha = f.get("sha256")
                rel = f.get("relative_path")
                if not sha or not rel:
                    continue
                try:
                    abs_path = str((project_root / rel).resolve())
                except OSError:
                    continue
                by_path.setdefault(cid, {})[abs_path] = sha
                by_name.setdefault(cid, {})[Path(rel).name] = sha
    for cid, frm, to in relocations:
        sha = by_path.get(cid, {}).get(frm) or by_name.get(cid, {}).get(Path(frm).name)
        if sha:
            by_path.setdefault(cid, {})[to] = sha
            by_name.setdefault(cid, {})[Path(to).name] = sha
    return by_path, by_name


def verify_images_against_manifest(images) -> List[dict]:
    """Re-hash each image and compare it to its capture-time manifest sha256.

    Returns the list of mismatches: files whose bytes on disk differ from what was
    recorded when they were captured. Images with no capture_id, no manifest entry,
    or a missing file are skipped, as there is no capture-time fixity to check, and a
    missing file is a separate failure the caller handles. Read-only.
    """
    by_path, by_name = _build_manifest_index(config.settings.projects_dir.resolve())
    mismatches: List[dict] = []
    for img in images:
        if not img.capture_id:
            continue
        resolved = resolve_within_storage(img.file_path)
        if resolved is None or not resolved.exists():
            continue
        paths = by_path.get(img.capture_id)
        names = by_name.get(img.capture_id)
        expected = paths.get(str(resolved)) if paths else None
        if expected is None and names:
            expected = names.get(resolved.name)
        if expected is None:
            continue
        try:
            actual = _sha256(resolved)
        except OSError:
            continue
        if actual != expected:
            mismatches.append({
                "record_image_id": img.id,
                "record_id": img.record_id,
                "file_path": img.file_path,
                "expected_sha256": expected,
                "actual_sha256": actual,
            })
    return mismatches


def run_integrity_check(db: Session, verify_hashes: bool = True,
                        max_hash_checks: Optional[int] = None) -> dict:
    """Reconcile the database, the image files, and the capture manifest.

    verify_hashes recomputes sha256 for manifested files (the expensive part);
    max_hash_checks caps how many are hashed (None = all). Returns a report dict
    with a summary and per-category detail lists.
    """
    images = db.query(RecordImage).all()

    referenced: set = set()
    missing_files: List[dict] = []
    missing_thumbnails: List[dict] = []

    for img in images:
        resolved = resolve_within_storage(img.file_path)
        if resolved is None or not resolved.exists():
            missing_files.append({
                "record_image_id": img.id,
                "record_id": img.record_id,
                "file_path": img.file_path,
            })
        else:
            referenced.add(str(resolved))
        if img.thumbnail_path:
            th = resolve_within_storage(img.thumbnail_path)
            if th is None or not th.exists():
                missing_thumbnails.append({
                    "record_image_id": img.id,
                    "thumbnail_path": img.thumbnail_path,
                })
            else:
                referenced.add(str(th))

    # Manifest index is cheap (no hashing) and is needed both to spot-check hashes
    # and to recognize legitimate captured sidecars (e.g. raw files) that have no
    # dedicated row, so it is always built.
    by_path, by_name = _build_manifest_index(config.settings.projects_dir.resolve())
    manifest_paths: set = set()
    for paths in by_path.values():
        manifest_paths.update(paths.keys())
    known = referenced | manifest_paths
    # A RAW master's `_preview.jpg` sidecar has no row and no manifest entry of
    # its own, so it counts as known when its master is known and still on
    # disk. The manifest is append-only and keeps the lines of deleted
    # captures, so a known path alone does not mean the master exists; a
    # sidecar whose master is gone is the orphan a delete that ignored
    # sidecars leaves (NEH-257), and it is reported.
    known |= {
        str(preview_sidecar_of(Path(k)))
        for k in known
        if is_raw_suffix(Path(k)) and Path(k).is_file()
    }

    # files -> rows: image files known to neither the database nor the manifest
    orphan_files: List[str] = []
    for root in _scan_roots():
        for p in _iter_image_files(root):
            try:
                rp = str(p.resolve())
            except OSError:
                continue
            if rp not in known:
                orphan_files.append(rp)

    # bytes vs capture-time manifest hashes
    manifest_mismatches: List[dict] = []
    manifest_missing_entry: List[dict] = []
    hashes_checked = 0
    hashes_eligible = 0
    if verify_hashes:
        # Finding the manifest entry is cheap, so it runs over every image (a
        # missing entry is worth reporting for all). Only the sha256 recompute is
        # expensive, so when capped it hashes a random sample of the eligible
        # files rather than the first N - a bounded but representative sweep.
        eligible: List[Tuple] = []
        for img in images:
            if not img.capture_id:
                continue
            resolved = resolve_within_storage(img.file_path)
            if resolved is None or not resolved.exists():
                continue  # already reported under missing_files
            paths = by_path.get(img.capture_id)
            names = by_name.get(img.capture_id)
            expected = paths.get(str(resolved)) if paths else None
            if expected is None and names:
                expected = names.get(resolved.name)
            if expected is None:
                manifest_missing_entry.append({
                    "record_image_id": img.id,
                    "capture_id": img.capture_id,
                    "file_path": img.file_path,
                })
                continue
            eligible.append((img, resolved, expected))

        hashes_eligible = len(eligible)
        to_hash = eligible
        if max_hash_checks is not None and hashes_eligible > max_hash_checks:
            to_hash = random.sample(eligible, max_hash_checks)

        for img, resolved, expected in to_hash:
            try:
                actual = _sha256(resolved)
            except OSError:
                continue
            hashes_checked += 1
            if actual != expected:
                manifest_mismatches.append({
                    "record_image_id": img.id,
                    "file_path": img.file_path,
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                })

    orphan_records = [
        {"record_id": r.id, "title": r.title}
        for r in db.query(Record).filter(
            Record.project_id.is_(None), Record.collection_id.is_(None)
        ).all()
    ]

    summary = {
        "db_images": len(images),
        "missing_files": len(missing_files),
        "missing_thumbnails": len(missing_thumbnails),
        "orphan_files": len(orphan_files),
        "hashes_checked": hashes_checked,
        "hashes_eligible": hashes_eligible,
        "hash_sampled": hashes_checked < hashes_eligible,
        "manifest_mismatches": len(manifest_mismatches),
        "manifest_missing_entry": len(manifest_missing_entry),
        "orphan_records": len(orphan_records),
    }
    # "ok" reflects hard divergences (data loss or corruption). Missing thumbnails
    # are regenerable and a missing manifest entry is often benign (uploads have no
    # capture_id), so they are reported but do not by themselves fail the check.
    summary["ok"] = (
        summary["missing_files"] == 0
        and summary["orphan_files"] == 0
        and summary["manifest_mismatches"] == 0
    )

    return {
        "summary": summary,
        "missing_files": missing_files,
        "missing_thumbnails": missing_thumbnails,
        "orphan_files": orphan_files,
        "manifest_mismatches": manifest_mismatches,
        "manifest_missing_entry": manifest_missing_entry,
        "orphan_records": orphan_records,
    }
