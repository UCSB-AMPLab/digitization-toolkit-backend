"""Backfill the sha256 of RAW previews captured before capture recorded it.

Capture lists a RAW master's `_preview.jpg` in its capture record
(capture.manifestHandler._preview_file). A preview whose capture record does
not list it has no digest anywhere, and Fisqua's pipeline reads the preview
and checks its sha256 at pull (exchange contract 1.1, §4), so a volume of them cannot be
exported. This computes each one from the local file and appends it to the
volume's own `metadata/manifest.jsonl` as a `preview_digest` record:

    {"record_type": "preview_digest", "capture_id", "image_id",
     "files": [{"role": "<role>_preview", "relative_path", "bytes",
                "mimetype": "image/jpeg", "sha256"}],
     "computed_at", "backfilled": true}

the path relative to the volume's directory, as a capture record's is. The
`files` shape is the capture record's, so the fixity index reads the entry
as it reads a preview recorded at capture, and the startup migration rebases
it like any other line it copies. Existing lines are never rewritten.

A digest computed now is a digest of the file as it is now, not as it was
shot. Where the preview has been uploaded, its offsite row holds the SHA1 of
the bytes uploaded, so the file is backfilled only when its SHA1 is still
that one: the digest is then of the bytes in the bucket. A preview whose
SHA1 differs is reported and left without a digest. A preview with no
offsite row has not been uploaded, and the uploader will send the bytes
that were hashed (or report a different SHA1 at the key).

Every image of the volume, current or superseded, with a RAW master is
considered. Reported, and not backfilled: a preview the manifests already
give a digest for (`already_recorded`), one with no capture id
(`no_capture_id`), one whose file has been freed (`freed`:
record_images.preview_freed_at, set by app.core.offsite_free) or is
otherwise not on disk (`missing`), one outside the volume's directory (`outside_volume`),
one whose file changed while it was read (`unreadable`), one whose row is
gone, has left the volume or names another file by the time it is read
(`changed`), and one whose SHA1 differs from its offsite row
(`sha1_differs`).

Run as `python -m app.core.image_upload backfill-preview-digests <stem>`,
a dry run unless `--apply`. It runs in a process of its own, outside the
backend's read hold. As adoption does (app.core.image_upload.adopt_objects),
the image's row is read again before the hash, and the path and capture id
come from that read, not from the listing query; the file is identified by
its inode on both sides of the hash, and the row must read the same
afterwards.

Version: 0.1.0-beta
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.core import config

ACTION = "preview_digests_backfilled"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backfill_preview_digests(db: Session, stem: str, *, apply: bool = False) -> dict:
    """Record a `preview_digest` for each preview of the volume that has no digest (module header).

    Raises ValueError for a stem no volume has. Without `apply` nothing is
    written; the report says what would be.
    """
    from capture.manifestHandler import PREVIEW_DIGEST_RECORD_TYPE, append_manifest_lines

    from app.core import audit
    from app.core.image_upload import _image_state, stable_digests
    from app.core.integrity import build_fixity_index
    from app.core.paths import is_raw_suffix, preview_sidecar_of, resolve_within_storage
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    stem = (stem or "").strip()
    collection = db.query(Collection).filter(Collection.filename_stem == stem).one_or_none() if stem else None
    if collection is None:
        raise ValueError(f"no volume has the file name stem {stem!r}")

    projects_dir = config.settings.projects_dir.resolve()
    volume_dir = projects_dir / stem
    index = build_fixity_index(projects_dir)

    images = (db.query(RecordImage)
              .join(Record, RecordImage.record_id == Record.id)
              .filter(Record.collection_id == collection.id)
              .order_by(RecordImage.id)
              .all())
    rows = {r.record_image_id: r for r in db.query(OffsiteObject).filter(
        OffsiteObject.kind == "preview",
        OffsiteObject.record_image_id.in_([i.id for i in images] or [-1]))}

    report = {"stem": stem, "applied": apply, "backfilled": [], "already_recorded": 0,
              "no_capture_id": [], "freed": [], "missing": [], "outside_volume": [],
              "unreadable": [], "changed": [], "sha1_differs": []}
    lines = []
    for img in images:
        if not img.file_path or not is_raw_suffix(Path(img.file_path)):
            continue
        entry = {"image_id": img.id, "record_id": img.record_id, "current": bool(img.is_current)}
        # The query above is a snapshot. The row is read again, and only
        # that read is used: it must still exist and still be in this
        # volume, and its path and capture id are the ones that count.
        state = _image_state(db, img.id)
        if state is None or state.collection_id != collection.id:
            report["changed"].append(entry)
            continue
        if not state.file_path or not is_raw_suffix(Path(state.file_path)):
            report["changed"].append(entry)
            continue
        if not state.capture_id:
            report["no_capture_id"].append(entry)
            continue
        master = resolve_within_storage(state.file_path)
        preview = resolve_within_storage(str(preview_sidecar_of(master))) if master is not None else None
        if preview is None:
            report["outside_volume"].append(entry)
            continue
        entry["path"] = str(preview)
        if index.expected(state.capture_id, None, preview) is not None:
            report["already_recorded"] += 1
            continue
        # Read now, as the state is: freeing may have run since the listing.
        freed_at = (db.query(RecordImage.preview_freed_at)
                    .filter(RecordImage.id == img.id).scalar())
        if freed_at is not None:
            report["freed"].append(entry)
            continue
        if not preview.is_file():
            report["missing"].append(entry)
            continue
        if not preview.is_relative_to(volume_dir):
            report["outside_volume"].append(entry)
            continue

        try:
            before = preview.stat()
            digests = stable_digests(preview)
            after = preview.stat()
        except OSError:
            digests = None
        if digests is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            report["unreadable"].append(entry)
            continue
        if _image_state(db, img.id) != state:
            report["changed"].append(entry)
            continue
        sha1, sha256 = digests

        row = rows.get(img.id)
        if row is not None and row.local_sha1 != sha1:
            report["sha1_differs"].append({**entry, "offsite_key": row.object_key,
                                           "offsite_sha1": row.local_sha1, "local_sha1": sha1})
            continue

        computed_at = _now().isoformat()
        relative = str(preview.relative_to(volume_dir))
        lines.append(json.dumps({
            "record_type": PREVIEW_DIGEST_RECORD_TYPE,
            "capture_id": state.capture_id,
            "image_id": img.id,
            "files": [{"role": f"{img.role or 'single'}_preview", "relative_path": relative,
                       "bytes": after.st_size, "mimetype": "image/jpeg", "sha256": sha256}],
            "computed_at": computed_at,
            "backfilled": True,
        }, ensure_ascii=False))
        report["backfilled"].append({**entry, "relative_path": relative, "sha256": sha256,
                                     "bytes": after.st_size, "checked_against_offsite": row is not None})
    db.commit()  # no transaction stays open from the reads

    if apply:
        append_manifest_lines(volume_dir, lines)
        audit.log_event(db, level="INFO", category="system", action=ACTION, actor=None, subject=stem,
                        detail=(f"backfilled={len(report['backfilled'])} "
                                f"already_recorded={report['already_recorded']} "
                                f"sha1_differs={len(report['sha1_differs'])} freed={len(report['freed'])} "
                                f"missing={len(report['missing'])} unreadable={len(report['unreadable'])} "
                                f"changed={len(report['changed'])}"))
    return report
