"""The work a background export job runs: validate, check disk, stream the bag,
prune old zips. Runs in a worker thread with its own DB session.
"""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.core import config
from app.core.bag_export import write_bag_zip
from app.core.export_jobs import ExportError, ExportJob
from app.core.integrity import verify_images_against_manifest
from app.core.paths import resolve_within_storage
from app.core.storage_ops import resolve_project_name
from app.models.collection import Collection
from app.models.project import Project
from app.models.record import Record

logger = logging.getLogger(__name__)

# Keep the most recent N export zips per collection; older ones are pruned so the
# SD does not fill up with archives no one downloads.
EXPORT_KEEP = 3
# Free-space safety margin on top of the estimated bag size.
FREE_SPACE_MARGIN = 100 * 1024 * 1024


def run_export(job: ExportJob, collection_id: int) -> None:
    from app.core.db import SessionLocal
    db = SessionLocal()
    try:
        _do_export(db, job, collection_id)
    finally:
        db.close()


def _do_export(db, job: ExportJob, collection_id: int) -> None:
    collection = db.query(Collection).filter(Collection.id == collection_id).first()
    if not collection:
        raise ExportError(f"Collection {collection_id} not found")

    records = (
        db.query(Record)
        .filter(Record.collection_id == collection_id)
        .order_by(Record.sequence.nulls_last(), Record.id)
        .all()
    )
    project = db.query(Project).filter(Project.id == collection.project_id).first() if collection.project_id else None

    exports_dir = config.settings.exports_dir
    exports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bag_name = f"collection_{collection_id}_{timestamp}"

    # Plan the payload: resolve every current image, refuse on any missing file, and give each a unique name so the payload cannot disagree with metadata.json.
    to_copy = []   # (rel_under_data, resolved_src, img)
    missing = []
    used_names: dict = {}
    for idx, rec in enumerate(records):
        seq_label = f"{idx + 1:04d}"
        safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (rec.title or "record"))[:60]
        rec_dir_name = f"{seq_label}_{safe_title}"
        for img in sorted([i for i in rec.images if i.is_current], key=lambda i: (i.role or "z", i.id)):
            resolved = resolve_within_storage(img.file_path) if img.file_path else None
            if resolved is None or not resolved.exists():
                missing.append({"record_id": rec.id, "record_image_id": img.id, "file_path": img.file_path})
                continue
            role_prefix = img.role or f"img_{img.id}"
            dest_name = f"{role_prefix}{resolved.suffix}"
            seen = used_names.setdefault(rec_dir_name, set())
            if dest_name in seen:
                dest_name = f"{role_prefix}_{img.id}{resolved.suffix}"
            seen.add(dest_name)
            to_copy.append((f"{rec_dir_name}/{dest_name}", resolved, img))

    if missing:
        raise ExportError(
            f"{len(missing)} image file(s) missing from disk.",
            detail={"reason": "missing_files", "missing_image_ids": [m["record_image_id"] for m in missing], "missing": missing},
        )

    # Fixity gate: refuse to bag bytes that no longer match their capture-time sha256.
    mismatches = verify_images_against_manifest([img for _, _, img in to_copy])
    if mismatches:
        raise ExportError(
            f"{len(mismatches)} file(s) fail their capture-time checksum (possible corruption).",
            detail={"reason": "checksum_mismatch", "mismatched_image_ids": [m["record_image_id"] for m in mismatches], "mismatches": mismatches},
        )

    # Refuse to start if the exports filesystem can't hold the archive.
    total_src = sum(resolved.stat().st_size for _, resolved, _ in to_copy)
    free = shutil.disk_usage(exports_dir).free
    if free < total_src + FREE_SPACE_MARGIN:
        raise ExportError(
            "Not enough free space on the exports filesystem to build this archive.",
            detail={"reason": "low_disk", "needed_bytes": total_src + FREE_SPACE_MARGIN, "free_bytes": free},
        )

    metadata_payload = _metadata_payload(collection, project, records, timestamp)
    blobs = [(json.dumps(metadata_payload, indent=2, ensure_ascii=False).encode("utf-8"), "metadata.json")]
    manifest_blob = _filtered_manifest_blob(db, collection, records)
    if manifest_blob:
        blobs.append((manifest_blob, "metadata/manifest.jsonl"))

    payload_files = [(resolved, rel) for rel, resolved, _ in to_copy]
    bag_info = {
        "Source-Organization": project.name if project else "Digitization Toolkit",
        "External-Description": collection.description or collection.name,
        "Bagging-Date": timestamp[:8],
        "External-Identifier": f"collection-{collection_id}",
        "Bag-Count": "1 of 1",
        "Record-Count": str(len(records)),
    }

    # Write the zip straight into the exports dir, atomically via a .part file so a
    # crash mid-write never leaves a truncated zip that download would serve.
    job.set_progress(0, len(payload_files) + len(blobs))
    zip_path = exports_dir / f"{bag_name}.zip"
    part_path = exports_dir / f"{bag_name}.zip.part"
    try:
        write_bag_zip(part_path, bag_name, bag_info, payload_files, blobs,
                      progress_cb=job.set_progress)
        part_path.replace(zip_path)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    job.zip_filename = zip_path.name
    _prune_exports(exports_dir, collection_id)
    logger.info(f"BagIt export created: {zip_path} ({zip_path.stat().st_size} bytes)")


def _metadata_payload(collection, project, records, timestamp) -> dict:
    return {
        "exported_at": timestamp,
        "collection": {
            "id": collection.id,
            "name": collection.name,
            "description": collection.description,
            "collection_type": collection.collection_type,
            "archival_metadata": collection.archival_metadata,
            "created_by": collection.created_by,
            "created_at": collection.created_at.isoformat() if collection.created_at else None,
        },
        "project": {"id": project.id, "name": project.name} if project else None,
        "records": [
            {
                "id": r.id, "sequence": r.sequence, "title": r.title, "description": r.description,
                "object_typology": r.object_typology, "author": r.author, "material": r.material,
                "date": r.date, "status": r.status, "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "images": [
                    {
                        "id": img.id, "filename": img.filename, "role": img.role, "sequence": img.sequence,
                        "format": img.format, "resolution_width": img.resolution_width,
                        "resolution_height": img.resolution_height, "file_size": img.file_size,
                    }
                    for img in r.images if img.is_current
                ],
            }
            for r in records
        ],
    }


def _filtered_manifest_blob(db, collection, records):
    """The project's capture manifest, filtered to this collection's captures."""
    from capture.project_manager import secure_project_filename
    capture_ids = {img.capture_id for rec in records for img in rec.images if img.is_current and img.capture_id}
    if not capture_ids:
        return None
    proj_name = resolve_project_name(db, collection)
    if not proj_name:
        return None
    manifest_src = config.settings.projects_dir / secure_project_filename(proj_name) / "metadata" / "manifest.jsonl"
    if not manifest_src.exists():
        return None
    kept = []
    for line in manifest_src.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if entry.get("capture_id") in capture_ids:
            kept.append(stripped)
    return ("\n".join(kept) + "\n").encode("utf-8") if kept else None


def _prune_exports(exports_dir: Path, collection_id: int, keep: int = EXPORT_KEEP) -> None:
    """Keep the newest `keep` zips for this collection; delete older zips and any
    stale .part files left by an interrupted export."""
    for part in exports_dir.glob(f"collection_{collection_id}_*.zip.part"):
        part.unlink(missing_ok=True)
    zips = sorted(exports_dir.glob(f"collection_{collection_id}_*.zip"), reverse=True)
    for old in zips[keep:]:
        try:
            old.unlink()
        except OSError:
            logger.warning(f"Could not prune old export {old}")
