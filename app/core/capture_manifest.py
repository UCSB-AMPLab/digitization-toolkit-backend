"""A volume's capture manifest for Fisqua (exchange contract 1.1, §4, artefact 2).

Built only from the catalogue (the volume, its records and their current
images), the capture manifests (`metadata/manifest.jsonl`, read through
app.core.integrity.build_fixity_index) and offsite_objects. No image file is
read, so a volume whose local files have been freed exports the same as one
whose files are still on disk.

* `objectKey` and `previewObjectKey` are the frozen keys of the image's
  verified offsite objects, with the tenant prefix taken off: the contract
  makes them relative to `transfer.root`, which is `{tenant}/`. `filename`
  is the last segment of the capture key, so `objectKey` is
  `<stem>/captures/<filename>` as §4 shows it, whatever the file is called on
  disk now.
* `sha256` is the capture-time digest the capture manifests hold for the
  image (a relocation record naming the image, then its path, then its name,
  then the name in its key). `previewSha256` is looked up the same way for
  the preview's file. Neither is ever computed here: a missing digest refuses
  the export.
* Only current images are listed. A superseded image stays in the bucket and
  out of the manifest (§3: retakes stay in the volume and take the next
  number; §4: the new manifest replaces the volume's page list).
* Every record of the volume is listed, whatever its status (§8, ruled
  2026-10-17: the manifest carries every record's status).

The export is refused, with every problem named, when Fisqua's registration
(fisqua-dev scripts/lib/capture-registration.ts) would refuse the file or
reject the volume for a reason the unit can know, or when the keys are not
of the form §4 gives them. The checks are listed in RULES.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session, selectinload

from app.core import config
from app.core.paths import CAPTURES_DIR, PREVIEW_SIDECAR_SUFFIX, PREVIEWS_DIR, is_raw_suffix, preview_sidecar_of

CONTRACT = "fisqua-captua-exchange"
VERSION = "1.1"
KIND = "capture-manifest"
EXPORT_ACTION = "capture_manifest_exported"

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# Fisqua's PIPELINE_PAGE_KEY_PATTERN (fisqua-dev app/lib/validation/enums.ts).
PAGE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
VALID_LAYOUTS = (("single",), ("left",), ("right",), ("left", "right"))
ROLE_ORDER = {"left": 0, "right": 1, "single": 2}
HANDED_OVER = ("in_review", "approved")

# Rule codes, in the order problems are reported.
RULES = (
    "tenant_missing",
    "tenant_invalid",
    "no_stem",
    "no_project",
    "records_captured",
    "sequence_missing",
    "sequence_repeated",
    "roles_invalid",
    "capture_id_missing",
    "not_uploaded",
    "preview_not_uploaded",
    "upload_refused",
    "key_outside",
    "filename_form",
    "page_key_repeated",
    "sha256_missing",
    "preview_sha256_missing",
    "no_pages",
)

_LIST_LIMIT = 20


def _ids(ids) -> str:
    ids = sorted(set(ids))
    shown = ", ".join(str(i) for i in ids[:_LIST_LIMIT])
    return shown + (f" and {len(ids) - _LIST_LIMIT} more" if len(ids) > _LIST_LIMIT else "")


@dataclass
class Problem:
    rule: str
    message: str
    record_ids: List[int] = field(default_factory=list)
    image_ids: List[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"rule": self.rule, "message": self.message,
                "record_ids": sorted(set(self.record_ids)), "image_ids": sorted(set(self.image_ids))}


class ManifestRefused(Exception):
    """The volume's manifest would be refused by Fisqua; `problems` names each reason."""

    def __init__(self, problems: List[Problem]):
        order = {rule: n for n, rule in enumerate(RULES)}
        self.problems = sorted(problems, key=lambda p: order.get(p.rule, len(order)))
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return ("The capture manifest cannot be exported, because Fisqua would refuse it: "
                + " ".join(p.message for p in self.problems))


class _Collector:
    """Image and record ids per rule, turned into one Problem per rule at the end."""

    def __init__(self):
        self.records: Dict[str, set] = {}
        self.images: Dict[str, set] = {}
        self.notes: Dict[str, list] = {}

    def add(self, rule: str, record_id: int, image_id: Optional[int] = None, note: Optional[str] = None):
        self.records.setdefault(rule, set()).add(record_id)
        if image_id is not None:
            self.images.setdefault(rule, set()).add(image_id)
        if note is not None and note not in self.notes.setdefault(rule, []):
            self.notes[rule].append(note)

    def has(self, rule: str) -> bool:
        return rule in self.records


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key_name(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _resolved(path: str) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(path)


def _rejection_note(record) -> Optional[str]:
    """For a rejected record, its latest rejection's comment, or its reason when it has no comment."""
    if record.status != "rejected" or not record.rejections:
        return None
    latest = max(record.rejections, key=lambda r: (r.rejected_at, r.id))
    comment = (latest.comment or "").strip()
    return comment or latest.predefined_reason


def build_capture_manifest(db: Session, collection, *, now: Optional[datetime] = None,
                           new_id: Callable[[], uuid.UUID] = uuid.uuid4) -> dict:
    """The capture manifest of one volume, or ManifestRefused naming every problem (module header)."""
    from app.core.image_upload import ImageUploadMisconfigured, configured_tenant
    from app.core.integrity import build_fixity_index
    from app.core.storage_ops import resolve_project
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record

    problems: List[Problem] = []

    tenant = None
    try:
        tenant = configured_tenant()
        if tenant is None:
            problems.append(Problem(
                "tenant_missing",
                "No tenant is configured on this unit, so the manifest has no transfer root. "
                "Set DTK_OFFSITE_TENANT to the tenant's prefix and restart the backend."))
    except ImageUploadMisconfigured as exc:
        problems.append(Problem("tenant_invalid", f"{exc}. Correct it and restart the backend."))

    stem = collection.filename_stem
    if stem is None:
        problems.append(Problem(
            "no_stem",
            "The volume has no file name stem, so its images have no keys Fisqua can read. "
            "Give the volume its stem (its Fisqua reference code)."))

    project = resolve_project(db, collection)
    if project is None:
        problems.append(Problem(
            "no_project",
            "The volume is not in a project, and the manifest names its project. Move the volume into a project."))

    records = (
        db.query(Record)
        .options(selectinload(Record.images), selectinload(Record.rejections))
        .filter(Record.collection_id == collection.id)
        .order_by(Record.sequence.nulls_last(), Record.id)
        .all()
    )
    found = _Collector()

    by_sequence: Dict[int, List[int]] = {}
    for rec in records:
        if rec.status == "captured":
            found.add("records_captured", rec.id)
        if rec.sequence is None:
            found.add("sequence_missing", rec.id)
        else:
            by_sequence.setdefault(rec.sequence, []).append(rec.id)
    for ids in by_sequence.values():
        if len(ids) > 1:
            for rid in ids:
                found.add("sequence_repeated", rid)

    current = {rec.id: sorted((i for i in rec.images if i.is_current),
                              key=lambda i: (ROLE_ORDER.get(i.role, 3), i.id))
               for rec in records}
    image_ids = [i.id for imgs in current.values() for i in imgs]
    rows = {}
    for start in range(0, len(image_ids), 500):
        chunk = image_ids[start:start + 500]
        for row in db.query(OffsiteObject).filter(OffsiteObject.record_image_id.in_(chunk)):
            rows[(row.record_image_id, row.kind)] = row
    index = build_fixity_index(config.settings.projects_dir.resolve())

    capture_prefix = f"{tenant}/{stem}/{CAPTURES_DIR}/" if tenant and stem else None
    preview_prefix = f"{tenant}/{stem}/{PREVIEWS_DIR}/" if tenant and stem else None
    file_form = re.compile(rf"^{re.escape(stem)}-img_\d{{4,}}_c\d+\.[A-Za-z0-9]+$") if stem else None

    page_keys: Dict[str, List[tuple]] = {}
    out_records = []
    pages = 0
    for rec in records:
        imgs = current[rec.id]
        layout = tuple(sorted(i.role or "" for i in imgs))
        if imgs and layout not in VALID_LAYOUTS:
            found.add("roles_invalid", rec.id, note=f"record {rec.id}: {', '.join(i.role or 'no role' for i in imgs)}")
        elif not imgs and rec.status in HANDED_OVER:
            found.add("roles_invalid", rec.id, note=f"record {rec.id}: no image")
        if imgs and rec.status in HANDED_OVER:
            pages += len(imgs)

        out_images = []
        for img in imgs:
            entry = _image_entry(img, rec, rows, index, tenant, capture_prefix, preview_prefix,
                                 file_form, found, page_keys)
            if entry is not None:
                out_images.append(entry)
        out_records.append({
            "captuaRecordId": rec.id,
            "sequence": rec.sequence,
            "status": rec.status,
            "rejectionNote": _rejection_note(rec),
            "images": out_images,
        })

    for key, owners in page_keys.items():
        if len(owners) > 1:
            for rid, iid in owners:
                found.add("page_key_repeated", rid, iid, note=key)

    if pages == 0:
        problems.append(Problem(
            "no_pages",
            "No record under review or approved has an image, so Fisqua has no page to register. "
            "Hand over records with images."))

    problems.extend(_problems(found, tenant, stem))
    if problems:
        raise ManifestRefused(problems)

    stamp = (now or _now()).astimezone(timezone.utc)
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "kind": KIND,
        "id": str(new_id()),
        "generatedAt": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generatedBy": {"system": "captua", "instance": tenant,
                        "systemVersion": config.settings.app_version},
        # The unit does not record which digitisation list a volume came
        # from, so there is no list id to reply to.
        "inReplyTo": None,
        "project": {"captuaProjectId": project.id, "name": project.name},
        "transfer": {"channel": "upload", "root": f"{tenant}/"},
        "volumes": [{
            "externalId": collection.external_id,
            "captuaCollectionId": collection.id,
            "stem": stem,
            "title": collection.name,
            "records": out_records,
        }],
    }


def _object_state(row) -> str:
    if row is None:
        return "absent"
    if row.refused_at is not None:
        return "refused"
    if row.verified_at is not None:
        return "verified"
    return "pending"


def _digest(index, capture_id: str, image_id: Optional[int], path: Path, key_name: Optional[str]) -> Optional[str]:
    """The capture-time sha256 for a file, by FixityIndex.expected, then by the name in its key."""
    sha = index.expected(capture_id, image_id, path)
    if sha is None and key_name is not None and not (image_id is not None and (capture_id, image_id) in index.by_image):
        sha = index.by_name.get(capture_id, {}).get(key_name)
    return sha


def _image_entry(img, rec, rows, index, tenant, capture_prefix, preview_prefix, file_form, found, page_keys):
    """One image's manifest entry, or None when a problem was recorded for it."""
    ok = True
    if not img.capture_id:
        found.add("capture_id_missing", rec.id, img.id)
        ok = False

    cap = rows.get((img.id, "capture"))
    state = _object_state(cap)
    filename = None
    if state == "refused":
        found.add("upload_refused", rec.id, img.id, note=f"{cap.object_key} ({cap.refusal})")
        ok = False
    elif state != "verified":
        found.add("not_uploaded", rec.id, img.id)
        ok = False
    elif capture_prefix is not None:
        key = cap.object_key
        name = key[len(capture_prefix):] if key.startswith(capture_prefix) else None
        if not name or "/" in name:
            found.add("key_outside", rec.id, img.id, note=key)
            ok = False
        elif not file_form.match(name) or not PAGE_KEY_PATTERN.match(name.rsplit(".", 1)[0]):
            found.add("filename_form", rec.id, img.id, note=name)
            ok = False
        else:
            filename = name
            page_keys.setdefault(name.rsplit(".", 1)[0], []).append((rec.id, img.id))

    preview_key = None
    preview_path = None
    if is_raw_suffix(Path(img.file_path)):
        preview_path = preview_sidecar_of(_resolved(img.file_path))
        prow = rows.get((img.id, "preview"))
        pstate = _object_state(prow)
        if pstate == "refused":
            found.add("upload_refused", rec.id, img.id, note=f"{prow.object_key} ({prow.refusal})")
            ok = False
        elif pstate == "pending" or (pstate == "absent" and preview_path.is_file()):
            found.add("preview_not_uploaded", rec.id, img.id)
            ok = False
        elif pstate == "verified":
            preview_key = prow.object_key
            if filename is not None and preview_prefix is not None:
                wanted = f"{preview_prefix}{filename.rsplit('.', 1)[0]}{PREVIEW_SIDECAR_SUFFIX}"
                if preview_key != wanted:
                    found.add("key_outside", rec.id, img.id, note=preview_key)
                    ok = False

    if not img.capture_id:
        return None
    sha = _digest(index, img.capture_id, img.id, _resolved(img.file_path),
                  _key_name(cap.object_key) if cap is not None else None)
    if sha is None or not SHA256_PATTERN.match(sha):
        found.add("sha256_missing", rec.id, img.id)
        ok = False
    preview_sha = None
    if preview_key is not None:
        preview_sha = _digest(index, img.capture_id, None, preview_path, _key_name(preview_key))
        if preview_sha is None or not SHA256_PATTERN.match(preview_sha):
            found.add("preview_sha256_missing", rec.id, img.id)
            ok = False

    if not ok or tenant is None or filename is None:
        return None
    entry = {
        "captureId": img.capture_id,
        "pairId": img.pair_id,
        "filename": filename,
        "role": img.role,
        "objectKey": cap.object_key[len(tenant) + 1:],
        "sha256": sha,
    }
    if preview_key is not None:
        entry["previewObjectKey"] = preview_key[len(tenant) + 1:]
        entry["previewSha256"] = preview_sha
    return entry


def _problems(found: _Collector, tenant: Optional[str], stem: Optional[str]) -> List[Problem]:
    """One Problem per rule found, in plain English, naming the records and the fix."""
    texts = {
        "records_captured": lambda r, n: (
            f"{len(r)} record(s) are still captured, so the volume has not been handed over "
            f"(records {_ids(r)}). Hand the volume over (promote it) when its capture is finished."),
        "sequence_missing": lambda r, n: (
            f"{len(r)} record(s) have no sequence (records {_ids(r)}). Renumber the volume."),
        "sequence_repeated": lambda r, n: (
            f"Records {_ids(r)} share a sequence number. Renumber the volume."),
        "roles_invalid": lambda r, n: (
            "These records do not have one single image, or a left and/or a right image: "
            f"{'; '.join(n[:_LIST_LIMIT])}. Retake them so each has one single image or a left and right pair."),
        "capture_id_missing": lambda r, n: (
            f"{len(found.images['capture_id_missing'])} image(s) have no capture id, so the capture manifest "
            f"holds no checksum for them (records {_ids(r)}). Retake those records."),
        "not_uploaded": lambda r, n: (
            f"{len(found.images['not_uploaded'])} image(s) are not yet verified in the offsite bucket "
            f"(records {_ids(r)}). Wait for the offsite upload to verify them, then export again."),
        "preview_not_uploaded": lambda r, n: (
            f"The previews of {len(found.images['preview_not_uploaded'])} RAW image(s) are not yet verified "
            f"in the offsite bucket (records {_ids(r)}). Wait for the offsite upload to verify them, "
            "then export again."),
        "upload_refused": lambda r, n: (
            f"The offsite upload refused {len(n)} object(s) (records {_ids(r)}): {'; '.join(n[:_LIST_LIMIT])}. "
            "Resolve each refusal shown in the volume's offsite state, then export again."),
        "key_outside": lambda r, n: (
            f"{len(n)} object(s) are stored offsite under keys other than "
            f"{tenant}/{stem}/captures/<file> and {tenant}/{stem}/previews/<file>_preview.jpg "
            f"(records {_ids(r)}): {'; '.join(n[:_LIST_LIMIT])}. Offsite keys never change, so those "
            "records have to be captured again."),
        "filename_form": lambda r, n: (
            f"{len(n)} image(s) are stored offsite under names not of the form "
            f"{stem}-img_<nnnn>_c<n>.<ext> (records {_ids(r)}): {'; '.join(n[:_LIST_LIMIT])}. "
            "Offsite keys never change, so those records have to be captured again."),
        "page_key_repeated": lambda r, n: (
            f"More than one image is stored under the name {', '.join(n[:_LIST_LIMIT])} "
            f"(records {_ids(r)}), and Fisqua names each page by it. Those records have to be captured again."),
        "sha256_missing": lambda r, n: (
            f"The capture manifest holds no valid sha256 for {len(found.images['sha256_missing'])} image(s) "
            f"(records {_ids(r)}). The manifest is never built from recomputed digests; run the integrity "
            "check on the volume and resolve what it reports."),
        "preview_sha256_missing": lambda r, n: (
            f"No capture-time sha256 is recorded for the previews of "
            f"{len(found.images['preview_sha256_missing'])} RAW image(s) (records {_ids(r)}), and Fisqua's "
            "pipeline reads the preview. This unit does not record a preview's sha256 at capture, so a "
            "volume with RAW previews cannot be exported yet."),
    }
    out = []
    for rule in RULES:
        if rule in found.records:
            records = found.records[rule]
            notes = found.notes.get(rule, [])
            out.append(Problem(rule, texts[rule](records, notes), sorted(records),
                               sorted(found.images.get(rule, ()))))
    return out


def manifest_counts(doc: dict) -> dict:
    """Records, images and previews in a built manifest, for the system log."""
    records = doc["volumes"][0]["records"]
    images = [i for r in records for i in r["images"]]
    return {"records": len(records), "images": len(images),
            "previews": sum(1 for i in images if "previewObjectKey" in i)}


def download_name(stem: str, now: Optional[datetime] = None) -> str:
    """`capture-manifest_<stem>_<date>.json`: one file per volume, so two volumes of a project
    exported on the same day are not given the same name."""
    return f"capture-manifest_{stem}_{(now or _now()).strftime('%Y-%m-%d')}.json"
