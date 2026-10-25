"""The work a background export job runs: validate, check disk, stream the bag,
prune old zips. Runs in a worker thread with its own DB session.
"""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.core import config
from app.core.bag_export import SourceReadError, write_bag_zip
from app.core.export_jobs import ExportError, ExportJob
from app.core.integrity import capture_record_lines, verify_images_against_manifest
from app.core.paths import resolve_within_storage
from app.models.collection import Collection
from app.models.project import Project
from sqlalchemy.orm import joinedload

from app.models.record import Record

logger = logging.getLogger(__name__)

# Keep the most recent N export zips per collection; older ones are pruned so the
# SD does not fill up with archives no one downloads.
EXPORT_KEEP = 3
# Free-space safety margin on top of the estimated bag size.
FREE_SPACE_MARGIN = 100 * 1024 * 1024
# Shape of metadata.json inside an exported bag. Bump it whenever a key is
# added, removed or re-typed, so a receiving institution can tell which shape
# a bag holds. Bags already exported are never rewritten: a bag with no
# schema_version at all predates NEH-259, when the field was introduced
# alongside the removal of object_typology and material.
#
# 2: each image carries rotate_deg, the clockwise degrees (0, 90, 180, 270)
#    that display it upright, or null for an upload or an image captured
#    before the rotation was recorded. A RAW master is bagged as the camera
#    wrote it, so this is where its rotation is.
METADATA_SCHEMA_VERSION = 2


def workflow_blockers(records) -> tuple[str, list[int]] | None:
    """Why a collection's workflow state forbids an export, or None.

    One definition with two callers, deliberately: the request-time gate in
    app/api/collections.py and _do_export below. They are separated by however
    long the export queue is, and the worker reloads the records, so a check
    at request time alone would let a collection taken back in the gap export
    anyway, with the bag's metadata describing the work as `captured` (NEH-260).

    Handover is reported before approval because they are different things to
    tell an operator: a collection she has not handed over is work in progress,
    not work that failed review.

    An empty collection is reported here too, so the worker does not bag a
    collection whose last record was deleted after the request and write
    `"records": []` into the metadata.
    """
    if not records:
        return ("Collection has no records to export.", [])
    not_handed_over = [r.id for r in records if r.status == "captured"]
    if not_handed_over:
        return (
            f"Cannot export: {len(not_handed_over)} record(s) have not been handed over for review yet: {not_handed_over}",
            not_handed_over,
        )
    non_approved = [r.id for r in records if r.status != "approved"]
    if non_approved:
        return (
            f"Cannot export: {len(non_approved)} record(s) are not approved yet: {non_approved}",
            non_approved,
        )
    return None


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

    # Statuses and images are read in one statement, so the gate below and the
    # payload built further down see the same instant. Loading the images lazily
    # per record afterwards would let a retake land in between: the record's
    # cached status would still say `approved` while its image query returned
    # the replacement, and the bag would label a new unreviewed image as approved.
    records = (
        db.query(Record)
        .options(joinedload(Record.images))
        .filter(Record.collection_id == collection_id)
        .order_by(Record.sequence.nulls_last(), Record.id)
        .all()
    )
    # Re-checked here, not just at request time. Nothing is built before this.
    blocker = workflow_blockers(records)
    if blocker:
        message, blocking_ids = blocker
        raise ExportError(message, detail={"blocking_record_ids": blocking_ids})
    # What the gate passed on, kept so it can be checked again once the payload
    # is planned: an export is a statement about a state, and if that state
    # moved while the bag was being assembled the bag is not about anything.
    gated_state = {rec.id: (rec.status, rec.sequence,
                            tuple(sorted(i.id for i in rec.images if i.is_current)))
                   for rec in records}

    project = db.query(Project).filter(Project.id == collection.project_id).first() if collection.project_id else None

    exports_dir = config.settings.exports_dir
    exports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bag_name = f"collection_{collection_id}_{timestamp}"

    # Plan the payload: resolve every current image, refuse on any missing file, and give each a unique name so the payload cannot disagree with metadata.json.
    to_copy = []   # (rel_under_data, resolved_src, img)
    missing = []
    used_names: dict = {}
    # Re-checked in the worker, as the workflow gate is: a free can follow
    # the request. A freed master is stored offsite, not missing.
    freed = [i.id for rec in records for i in rec.images if i.is_current and i.master_freed_at is not None]
    if freed:
        from app.core.offsite_free import FREED_EXPORT_MESSAGE
        raise ExportError(FREED_EXPORT_MESSAGE.format(n=len(freed)),
                          detail={"reason": "freed_files", "freed_image_ids": freed})

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

    # From here to the end of the write, a planned source can be moved,
    # renamed or deleted (the routes that do it hold the collection against
    # exports, but a file can still go by other means: a drive pulled, a file
    # removed by hand), or fail to read (EIO from a drive that is going).
    # Either is reported as an ExportError naming the record, with the path in
    # the log only; see _source_error.
    try:
        _fixity_gate([img for _, _, img in to_copy])

        # Refuse to start if the exports filesystem can't hold the archive.
        total_src = sum(resolved.stat().st_size for _, resolved, _ in to_copy)
        free = shutil.disk_usage(exports_dir).free
        if free < total_src + FREE_SPACE_MARGIN:
            raise ExportError(
                "Not enough free space on the exports filesystem to build this archive.",
                detail={"reason": "low_disk", "needed_bytes": total_src + FREE_SPACE_MARGIN, "free_bytes": free},
            )

        # The payload is planned; nothing has been written yet. Check that the state
        # the gate passed on is still the state, because the bag is a statement about
        # it. A reviewer undoing an approval, or an operator retaking a record, in the
        # time it took to resolve the files leaves a bag that claims a review that no
        # longer stands, and so does a reorder or renumber, which would leave the
        # record directories numbered in the old order beside a metadata.json
        # carrying the new sequences. populate_existing() because a plain re-query returns the
        # instances already in the identity map without refreshing their columns,
        # which would compare the snapshot against itself.
        rechecked = (
            db.query(Record)
            .options(joinedload(Record.images))
            .filter(Record.collection_id == collection_id)
            .populate_existing()
            .all()
        )
        now_state = {rec.id: (rec.status, rec.sequence,
                              tuple(sorted(i.id for i in rec.images if i.is_current)))
                     for rec in rechecked}
        if now_state != gated_state:
            moved = sorted(
                set(gated_state) ^ set(now_state)
                | {rid for rid in set(gated_state) & set(now_state)
                   if gated_state[rid] != now_state[rid]}
            )
            raise ExportError(
                "Cannot export: the collection changed while the archive was being "
                f"assembled ({len(moved)} record(s) affected). Try again.",
                detail={"blocking_record_ids": moved},
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
    except SourceReadError as e:
        raise _source_error(collection_id, to_copy, e.source_path, e.error) from e
    except OSError as e:
        if e.filename is None or not any(str(r) == str(e.filename) for _, r, _ in to_copy):
            raise
        raise _source_error(collection_id, to_copy, Path(e.filename), e) from e

    job.zip_filename = zip_path.name
    _prune_exports(exports_dir, collection_id)
    logger.info(f"BagIt export created: {zip_path} ({zip_path.stat().st_size} bytes)")


def _fixity_gate(images) -> None:
    """Refuse to bag bytes that no longer match their capture-time sha256, or that cannot be checked.

    Raises ExportError with reason "checksum_mismatch" when a file's bytes
    differ from its capture record's, and otherwise with reason
    "checksum_unresolved" when a captured image (its capture id has a
    capture record in some manifest) has no checksum that can be resolved,
    naming each such image's id, capture id and stored path. Images with no
    capture record (uploads) pass unchecked.
    """
    found = verify_images_against_manifest(images)
    mismatches = [m for m in found if m["reason"] == "mismatch"]
    unresolved = [m for m in found if m["reason"] == "unresolved"]
    if mismatches:
        raise ExportError(
            f"{len(mismatches)} file(s) fail their capture-time checksum (possible corruption).",
            detail={"reason": "checksum_mismatch", "mismatched_image_ids": [m["record_image_id"] for m in mismatches], "mismatches": mismatches},
        )
    if unresolved:
        ids = [m["record_image_id"] for m in unresolved]
        raise ExportError(
            f"{len(unresolved)} captured file(s) have no capture-time checksum that can be found, "
            f"so their fixity cannot be checked: image(s) {', '.join(str(i) for i in ids)}.",
            detail={"reason": "checksum_unresolved", "unresolved_image_ids": ids,
                    "unresolved": [{"record_image_id": m["record_image_id"], "capture_id": m["capture_id"],
                                    "file_path": m["file_path"]} for m in unresolved]},
        )


def _source_error(collection_id: int, to_copy, source: Path, error: OSError) -> ExportError:
    """The ExportError for a planned source that is gone or could not be read.

    A missing file is the collection changing under the export, reported as
    the status re-check reports it. Any other error reading it is reported as
    unreadable files, which on this appliance most often means the storage
    drive. The path and the OS error go to the log.
    """
    record_ids = sorted({img.record_id for _, resolved, img in to_copy if str(resolved) == str(source)})
    if isinstance(error, FileNotFoundError):
        logger.warning(f"Export of collection {collection_id}: source file gone before it was read: {source}")
        return ExportError(
            "Cannot export: the collection changed while the archive was being "
            f"assembled ({len(record_ids)} record(s) affected). Try again.",
            detail={"blocking_record_ids": record_ids},
        )
    logger.error(f"Export of collection {collection_id}: source file could not be read: {source}: {error}")
    return ExportError(
        "Cannot export: some image files could not be read. Check the storage drive, then try again.",
        detail={"reason": "read_error", "blocking_record_ids": record_ids},
    )


def _metadata_payload(collection, project, records, timestamp) -> dict:
    return {
        "schema_version": METADATA_SCHEMA_VERSION,
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
                "author": r.author, "date": r.date, "status": r.status,
                # capture_mode is the only record of whether an item was shot
                # as one image or as an L+R pair, and a receiving institution
                # needs that to read the images (NEH-259).
                "capture_mode": r.capture_mode, "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "images": [
                    {
                        "id": img.id, "filename": img.filename, "role": img.role, "sequence": img.sequence,
                        "format": img.format, "resolution_width": img.resolution_width,
                        "resolution_height": img.resolution_height, "file_size": img.file_size,
                        "rotate_deg": img.rotate_deg,
                    }
                    for img in r.images if img.is_current
                ],
            }
            for r in records
        ],
    }


def _filtered_manifest_blob(db, collection, records):
    """The capture records of this collection's current images, from every capture manifest.

    The records are found the way the fixity gate finds checksums
    (app.core.integrity): in any manifest under the active projects root,
    the project's or the volume's own, so a volume migrated into its stem
    layout, whose earlier captures are recorded in its project's manifest
    and later ones in `<stem>/metadata/manifest.jsonl`, has all of them in
    its bag. Relocation records are the appliance's own bookkeeping of files
    renamed after capture; the bag names its files itself, so only the
    capture records go into it.
    """
    capture_ids = {img.capture_id for rec in records for img in rec.images if img.is_current and img.capture_id}
    if not capture_ids:
        return None
    kept = capture_record_lines(config.settings.projects_dir.resolve(), capture_ids)
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
