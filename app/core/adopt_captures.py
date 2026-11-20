"""The plan for adopting a hand-made volume's captures into an empty volume Fisqua assigned (NEH-387).

A volume made by hand on the unit and never delivered to Fisqua (the source)
holds captures that belong to a volume Fisqua's queue later assigned (the
target). Fisqua takes an image only under the target's own names: the file
`<target stem>-img_<nnnn>_c<n>.<ext>` (exchange contract 1.1 §3) and the key
`<tenant>/<target stem>/captures/<file>` (§4). The adoption renames each
source file by swapping the stem prefix and keeping the number, camera and
suffix, moves the source's records with all their images into the target,
and uploads every file again under its new key; the old keys are retired
and deleted from the bucket by hand, since the unit's bucket key can upload
and list but cannot read, copy or delete.

plan_adopt_captures says what the adoption would do and every reason it may
not run. It reads the catalogue, the disk, the source's capture manifest
and offsite_objects. It takes no row lock, writes no row, commits nothing,
reads with autoflush off so nothing pending in the session is flushed,
creates or touches no file, and calls no bucket.

Refusals are codes from REFUSAL_CODES, every one that applies, in that
order; `details` holds what each refers to. A source or target that does not
exist, or the same volume twice, ends the checks there. Without both stems,
or without a projects root that can be written, no image is checked and no
rename is built.

The record-state refusal is POST /collections/{id}/move-records' own
(app.api.collections.move_collection_records, through
app.api.records.is_movable): only a record still `captured` may change
volume. That route's refusal of records stored offsite is not applied: an
adoption exists to move them. Its refusal between a kept volume and one that
is not (kept_volumes.refuse_kept_move) is stricter here: neither volume may
be kept on the unit, since a kept volume's files are never uploaded.

Source and target may be in different projects. Nothing here depends on the
project: a stemmed volume's files are in `<projects root>/<stem>/` and its
keys under `<tenant>/<stem>/`. The plan carries both projects
(storage_ops.resolve_project); records in a volume carry no project of their
own (check_record_single_parent), so they change project with their volume.

Names and paths come from the helpers that build them for capture and
upload: capture_naming.capture_filename_index for `<stem>-img_<nnnn>`,
storage_ops.collection_image_dir and collection_root_dir for the volume's
`captures/` and root, paths.preview_sidecar_of and raw_sidecar_of for the
sidecars' names, and image_upload.capture_key and preview_key for the keys,
under the configured tenant. A sidecar is looked for in its directory's
listing, compared case-insensitively, and planned under the name it is
listed with, its prefix swapped as the master's is. A RAW master's preview
is uploaded with it; a JPEG master has none. A raw sensor buffer beside a
JPEG master (`.raw`, NEH-261) is renamed with its master; it has no row and
is never uploaded. Thumbnails are named `<uuid>_thumb.jpg`
(app.core.thumbnail), so they keep their names and move only from the
source's `thumbnails/` to the target's; one recorded elsewhere stays where
it is, and a missing one is derived again later, so it is reported
(`thumbnails_missing`) and given no move.

Every entry under the source's root that belongs to no source image (its
master, preview, buffer or thumbnail) is listed in `uncovered`, by the
directory it is in (`captures`, `previews`, `thumbnails`, `metadata`, or
`other` for anything else at the root). One in `captures/` or `previews/`
refuses the adoption; the rest are reported. `metadata/manifest.jsonl` is
the source's capture manifest and is never uncovered.

The manifest is read with app.core.integrity.read_manifest_lines and
capture.manifestHandler.is_capture_record, as the rebuild and the layout
migration read a volume's own manifest. Each image is matched to the
capture lines of its capture id, and refused when none names its master's
path relative to the volume's root. Capture lines whose capture id no image
has are reported, not refused.

Version: 0.3.3
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

# Every refusal, in the order plan_adopt_captures lists them.
REFUSAL_CODES = (
    "source_not_found",                 # no volume has the source id
    "target_not_found",                 # no volume has the target id
    "same_volume",                      # the source is the target
    "source_has_no_stem",
    "target_has_no_stem",
    "target_not_assigned",              # the target has no Fisqua reference code (external_id)
    "target_withdrawn",                 # the queue withdrew the target (queue_hidden_at)
    "source_sent_to_fisqua",            # a fisqua_outbox row in any state, or a fisqua_status row
    "source_kept_on_unit",
    "target_kept_on_unit",
    "source_has_sub_volumes",           # a collection has the source as its parent
    "target_has_records",
    "target_has_images",
    "target_counter_raised",            # capture_counter above 0
    "target_has_offsite_keys",          # an offsite_objects key `<any tenant>/<target stem>/...`
    "offsite_tenant_not_configured",    # no valid CAPTUA_OFFSITE_TENANT, so no new key can be built
    "storage_unavailable",              # the projects root cannot be written, so no new path can be built
    "source_records_handed_over",       # a record move-records would refuse (not `captured`)
    "source_files_freed",               # master_freed_at or preview_freed_at set
    "source_file_outside_own_folder",   # a master not in the source's own `captures/`
    "source_file_missing",              # a master, or a RAW master's preview, not on disk
    "source_not_verified_offsite",      # a capture, or a RAW master's preview, without a verified row
    "source_capture_line_missing",      # no capture line of the image's capture id names its master
    "source_name_not_contract",         # a file name that is not `<source stem>-img_<nnnn>_c<n>.<ext>`
    "source_has_unowned_files",         # a file in `captures/` or `previews/` no source image owns
    "new_name_collides",                # a new name or key already taken
)

MANIFEST_NAME = "manifest.jsonl"

# The directories under a volume's root in which a file no source image owns refuses the adoption.
_REFUSED_DIRS = ("captures", "previews")


@dataclass
class ImageRename:
    """One source image's names before and after; a path is None where the image has no such file."""
    image_id: int
    record_id: int
    is_current: bool
    number: int
    old_name: str
    new_name: str
    old_path: Path
    new_path: Path
    old_preview_path: Optional[Path] = None
    new_preview_path: Optional[Path] = None
    old_raw_buffer_path: Optional[Path] = None
    new_raw_buffer_path: Optional[Path] = None
    old_thumbnail_path: Optional[Path] = None
    new_thumbnail_path: Optional[Path] = None   # None for a missing thumbnail: no move is planned
    old_pair_id: Optional[str] = None
    new_pair_id: Optional[str] = None
    capture_id: Optional[str] = None
    capture_line: bool = False                  # a capture line of capture_id names the master
    old_keys: dict = field(default_factory=dict)   # kind -> the offsite_objects key, any state
    new_keys: dict = field(default_factory=dict)   # kind -> the key the upload would write


@dataclass
class AdoptPlan:
    source_id: int
    target_id: int
    source_stem: Optional[str] = None
    target_stem: Optional[str] = None
    source_project_id: Optional[int] = None
    target_project_id: Optional[int] = None
    refusals: list = field(default_factory=list)
    details: dict = field(default_factory=dict)
    renames: list = field(default_factory=list)
    records: int = 0
    images_current: int = 0
    images_superseded: int = 0
    # Bytes on disk of the files that would be renamed; thumbnails are not counted.
    bytes: dict = field(default_factory=lambda: {"masters": 0, "previews": 0, "raw_buffers": 0})
    counter_before: int = 0
    counter_after: int = 0
    uncovered: list = field(default_factory=list)            # {path, kind}, by path
    thumbnails_missing: list = field(default_factory=list)   # {image_id, path}
    capture_lines: int = 0                                   # capture lines in the source's manifest
    unaccounted_capture_lines: list = field(default_factory=list)   # their capture ids no image has

    @property
    def may_adopt(self) -> bool:
        return not self.refusals

    @property
    def project_changes(self) -> bool:
        return self.source_project_id != self.target_project_id

    def refuse(self, code: str, detail=None) -> None:
        if code not in self.refusals:
            self.refusals.append(code)
        if detail is not None:
            self.details.setdefault(code, []).append(detail)

    def _order(self) -> None:
        self.refusals.sort(key=REFUSAL_CODES.index)


def _contract_name(stem: str) -> "re.Pattern":
    """`<stem>-img_<nnnn>_c<n>.<ext>`, with the number, the camera and the suffix as groups."""
    return re.compile(r"^" + re.escape(stem) + r"-img_(\d{4})_c(\d+)(\.[A-Za-z0-9]+)$")


def _size(path: Optional[Path]) -> int:
    try:
        return path.stat().st_size if path is not None else 0
    except OSError:
        return 0


def _on_disk(path) -> bool:
    from app.core.paths import resolve_within_storage

    resolved = resolve_within_storage(str(path)) if path is not None else None
    return resolved is not None and resolved.is_file()


def _list_dir(directory: Path) -> list:
    """The entries of `directory`, as paths under it; none for a directory that cannot be listed."""
    try:
        with os.scandir(directory) as entries:
            return [Path(e.path) for e in entries]
    except OSError:
        return []


class _Listings:
    """Each directory's listing, read once, with its files by casefolded name."""

    def __init__(self):
        self._by_dir = {}

    def find(self, path: Path) -> Optional[Path]:
        """The listed file whose name is `path`'s name, compared case-insensitively, or None."""
        directory = path.parent
        if directory not in self._by_dir:
            self._by_dir[directory] = {p.name.casefold(): p for p in _list_dir(directory) if p.is_file()}
        return self._by_dir[directory].get(path.name.casefold())


def _swap_prefix(name: str, old: str, new: str) -> str:
    """`name` with its first len(old) characters, `old` in any case, replaced by `new`."""
    return new + name[len(old):]


def plan_adopt_captures(db: Session, source_id: int, target_id: int) -> AdoptPlan:
    """What adopting the source volume's captures into the target would do, and every refusal. Changes nothing."""
    with db.no_autoflush:
        return _plan(db, source_id, target_id)


def _plan(db: Session, source_id: int, target_id: int) -> AdoptPlan:
    from app.core.storage_ops import resolve_project
    from app.models.collection import Collection

    plan = AdoptPlan(source_id, target_id)
    source = db.get(Collection, source_id)
    target = db.get(Collection, target_id)
    if source is None:
        plan.refuse("source_not_found")
    if target is None:
        plan.refuse("target_not_found")
    if source is not None and target is not None and source.id == target.id:
        plan.refuse("same_volume")
    if plan.refusals:
        return plan

    plan.source_stem, plan.target_stem = source.filename_stem, target.filename_stem
    src_project, tgt_project = resolve_project(db, source), resolve_project(db, target)
    plan.source_project_id = src_project.id if src_project else None
    plan.target_project_id = tgt_project.id if tgt_project else None
    plan.counter_before = target.capture_counter or 0
    _check_volumes(db, plan, source, target)
    _plan_images(db, plan, source, target)
    plan._order()
    return plan


def _check_volumes(db: Session, plan: AdoptPlan, source, target) -> None:
    from app.core.image_upload import configured_tenant_or_none
    from app.models.collection import Collection, offsite_keys_under_stem
    from app.models.fisqua import FisquaOutbox, FisquaStatus
    from app.models.record import Record

    if not source.filename_stem:
        plan.refuse("source_has_no_stem")
    if not target.filename_stem:
        plan.refuse("target_has_no_stem")
    if target.external_id is None:
        plan.refuse("target_not_assigned")
    if target.queue_hidden_at is not None:
        plan.refuse("target_withdrawn")
    sent = (db.query(FisquaOutbox.id).filter(FisquaOutbox.collection_id == source.id).first() is not None
            or db.query(FisquaStatus.collection_id).filter(FisquaStatus.collection_id == source.id).first()
            is not None)
    if sent:
        plan.refuse("source_sent_to_fisqua")
    if source.kept_on_unit:
        plan.refuse("source_kept_on_unit")
    if target.kept_on_unit:
        plan.refuse("target_kept_on_unit")
    children = [cid for (cid,) in db.query(Collection.id).filter(Collection.parent_collection_id == source.id)
                .order_by(Collection.id)]
    if children:
        plan.refuse("source_has_sub_volumes")
        plan.details["source_has_sub_volumes"] = children
    if db.query(Record.id).filter(Record.collection_id == target.id).first() is not None:
        plan.refuse("target_has_records")
    if target.has_images():
        plan.refuse("target_has_images")
    if (target.capture_counter or 0) > 0:
        plan.refuse("target_counter_raised")
    if target.filename_stem and offsite_keys_under_stem(db, target.filename_stem):
        plan.refuse("target_has_offsite_keys")
    if configured_tenant_or_none() is None:
        plan.refuse("offsite_tenant_not_configured")


def _capture_lines(manifest: Path) -> list:
    """The capture lines of a volume's manifest, decoded; a torn or undecodable line is skipped."""
    from app.core.integrity import read_manifest_lines
    from capture.manifestHandler import is_capture_record

    if not manifest.is_file():
        return []
    try:
        lines, _ = read_manifest_lines(manifest)
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line) if line.strip() else None
        except json.JSONDecodeError:
            continue
        if is_capture_record(entry) and entry.get("capture_id"):
            out.append(entry)
    return out


def _named_paths(entry: dict) -> set:
    return {os.path.normpath(f["relative_path"]) for f in entry.get("files") or []
            if isinstance(f, dict) and isinstance(f.get("relative_path"), str)}


def _plan_images(db: Session, plan: AdoptPlan, source, target) -> None:
    from app.api.records import is_movable
    from app.core import image_upload
    from app.core.capture_naming import capture_filename_index
    from app.core.paths import METADATA_DIR, THUMBNAILS_DIR, is_raw_suffix, preview_sidecar_of, raw_sidecar_of
    from app.core.storage_ops import collection_image_dir, collection_root_dir
    from app.core.storage_override import StorageUnavailableError
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    records = db.query(Record).filter(Record.collection_id == source.id).order_by(Record.id).all()
    images = (db.query(RecordImage).join(Record, RecordImage.record_id == Record.id)
              .filter(Record.collection_id == source.id).order_by(RecordImage.id).all())
    rows = {}
    ids = [img.id for img in images]
    for start in range(0, len(ids), 500):
        for row in db.query(OffsiteObject).filter(OffsiteObject.record_image_id.in_(ids[start:start + 500])):
            rows[(row.record_image_id, row.kind)] = row

    plan.records = len(records)
    plan.images_current = sum(1 for img in images if img.is_current)
    plan.images_superseded = len(images) - plan.images_current
    handed_over = [r.id for r in records if not is_movable(r)]
    if handed_over:
        plan.refuse("source_records_handed_over")
        plan.details["source_records_handed_over"] = handed_over
    for img in images:
        if img.master_freed_at is not None:
            plan.refuse("source_files_freed", {"image_id": img.id, "kind": "master"})
        if img.preview_freed_at is not None:
            plan.refuse("source_files_freed", {"image_id": img.id, "kind": "preview"})

    if not (source.filename_stem and target.filename_stem):
        return
    src_stem, tgt_stem = source.filename_stem, target.filename_stem
    try:
        src_captures = collection_image_dir(db, source)
        tgt_captures = collection_image_dir(db, target)
        src_root = collection_root_dir(db, source)
        tgt_root = collection_root_dir(db, target)
    except StorageUnavailableError:
        plan.refuse("storage_unavailable")
        return
    tenant = image_upload.configured_tenant_or_none()
    pattern = _contract_name(src_stem)
    listings = _Listings()
    src_masters = [Path(img.file_path) for img in images]
    owned = set()   # the resolved paths of every file a source image owns

    lines = _capture_lines(src_root / METADATA_DIR / MANIFEST_NAME)
    plan.capture_lines = len(lines)
    named = {}
    for entry in lines:
        named.setdefault(entry["capture_id"], set()).update(_named_paths(entry))
    image_capture_ids = {img.capture_id for img in images if img.capture_id}
    plan.unaccounted_capture_lines = sorted({e["capture_id"] for e in lines} - image_capture_ids)

    for img in images:
        master = Path(img.file_path)
        raw = is_raw_suffix(master)
        owned.add(_resolved(master))
        try:
            outside = master.resolve().parent != src_captures.resolve()
        except (OSError, ValueError):
            outside = True
        if outside:
            plan.refuse("source_file_outside_own_folder", {"image_id": img.id, "path": img.file_path})

        derived_preview = preview_sidecar_of(master) if raw else None
        preview = listings.find(derived_preview) if raw else None
        if preview is not None:
            owned.add(_resolved(preview))
        buffer = None if raw else _raw_buffer(db, listings, master, src_masters)
        if buffer is not None:
            owned.add(_resolved(buffer))
        thumb = Path(img.thumbnail_path) if img.thumbnail_path else None
        thumb_present = thumb is not None and _on_disk(thumb)
        if thumb_present:
            owned.add(_resolved(thumb))
        elif thumb is not None:
            plan.thumbnails_missing.append({"image_id": img.id, "path": img.thumbnail_path})

        if img.master_freed_at is None and not _on_disk(master):
            plan.refuse("source_file_missing", {"image_id": img.id, "kind": "master", "path": img.file_path})
        if raw and img.preview_freed_at is None and preview is None:
            plan.refuse("source_file_missing", {"image_id": img.id, "kind": "preview",
                                                "path": str(derived_preview)})

        for kind in ("capture", "preview") if raw else ("capture",):
            row = rows.get((img.id, kind))
            if row is None or row.verified_at is None:
                plan.refuse("source_not_verified_offsite", {"image_id": img.id, "kind": kind})

        relative = os.path.normpath(os.path.relpath(master, src_root))
        has_line = bool(img.capture_id) and relative in named.get(img.capture_id, set())
        if not has_line:
            plan.refuse("source_capture_line_missing", {"image_id": img.id, "capture_id": img.capture_id})

        m = pattern.match(img.filename or "")
        if m is None or master.name != img.filename or int(m.group(1)) < 1:
            plan.refuse("source_name_not_contract", {"image_id": img.id, "filename": img.filename})
            continue
        number, camera, suffix = int(m.group(1)), m.group(2), m.group(3)
        new_name = f"{capture_filename_index(tgt_stem, number)}_c{camera}{suffix}"
        new_master = tgt_captures / new_name
        old_index = capture_filename_index(src_stem, number)
        rename = ImageRename(
            image_id=img.id, record_id=img.record_id, is_current=bool(img.is_current), number=number,
            old_name=img.filename, new_name=new_name, old_path=master, new_path=new_master,
            old_pair_id=img.pair_id,
            new_pair_id=capture_filename_index(tgt_stem, number) if img.pair_id == old_index else img.pair_id,
            capture_id=img.capture_id, capture_line=has_line,
            old_keys={kind: row.object_key for (image_id, kind), row in sorted(rows.items()) if image_id == img.id},
        )
        if raw:
            rename.old_preview_path = preview if preview is not None else derived_preview
            rename.new_preview_path = preview_sidecar_of(new_master).with_name(
                _swap_prefix(rename.old_preview_path.name, master.stem, new_master.stem))
        if buffer is not None:
            rename.old_raw_buffer_path = buffer
            rename.new_raw_buffer_path = raw_sidecar_of(new_master).with_name(
                _swap_prefix(buffer.name, master.stem, new_master.stem))
        if thumb is not None:
            rename.old_thumbnail_path = thumb
            if thumb_present:
                in_source = _within(thumb, src_root / THUMBNAILS_DIR)
                rename.new_thumbnail_path = tgt_root / THUMBNAILS_DIR / thumb.name if in_source else thumb
        if tenant is not None:
            rename.new_keys["capture"] = image_upload.capture_key(tenant, tgt_stem, new_name)
            if raw:
                rename.new_keys["preview"] = image_upload.preview_key(tenant, tgt_stem, rename.new_preview_path.name)
        plan.bytes["masters"] += _size(master)
        plan.bytes["previews"] += _size(preview)
        plan.bytes["raw_buffers"] += _size(buffer)
        plan.renames.append(rename)

    if not any(code == "source_not_verified_offsite" for code in plan.refusals):
        state = image_upload.volume_upload_state(db, source)
        if state["total"] and not state["received"]:
            plan.refuse("source_not_verified_offsite", {"volume": source.id, "total": state["total"],
                                                        "verified": state["verified"]})

    _uncovered(plan, src_root, owned)
    highest = max((r.number for r in plan.renames), default=0)
    plan.counter_after = max(plan.counter_before, source.capture_counter or 0, highest)
    _check_collisions(db, plan, tgt_stem, tgt_captures, tgt_root)


def _resolved(path: Path) -> str:
    try:
        return str(path.resolve())
    except (OSError, ValueError):
        return str(path)


def _within(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except (OSError, ValueError):
        return False


def _raw_buffer(db: Session, listings: _Listings, master: Path, src_masters: list) -> Optional[Path]:
    """The `.raw` buffer beside a JPEG master, as listed, when no row owns it (the rule of paths.raw_buffer_of)."""
    from app.core.paths import raw_sidecar_of, same_file
    from app.core.storage_ops import raw_buffer_candidate_names, rows_master_paths_named

    candidate = listings.find(raw_sidecar_of(master))
    if candidate is None:
        return None
    owners = list(src_masters) + rows_master_paths_named(db, raw_buffer_candidate_names(candidate))
    if any(same_file(candidate, m) for m in owners):
        return None
    return candidate


def _uncovered(plan: AdoptPlan, src_root: Path, owned: set) -> None:
    """List every entry under the source's root no source image owns, and refuse those in captures/ or previews/."""
    from app.core.paths import METADATA_DIR, VOLUME_SUBDIRS

    found = []
    for entry in _list_dir(src_root):
        if entry.name in VOLUME_SUBDIRS and entry.is_dir():
            for inner in _list_dir(entry):
                if entry.name == METADATA_DIR and inner.name == MANIFEST_NAME:
                    continue
                if _resolved(inner) not in owned:
                    found.append({"path": str(inner), "kind": entry.name})
        else:
            found.append({"path": str(entry), "kind": "other"})
    plan.uncovered = sorted(found, key=lambda e: e["path"])
    for e in plan.uncovered:
        if e["kind"] in _REFUSED_DIRS:
            plan.refuse("source_has_unowned_files", e)


def _check_collisions(db: Session, plan: AdoptPlan, tgt_stem: str, tgt_captures: Path, tgt_root: Path) -> None:
    """Refuse every new name or key already taken on disk, by a record_images row, by a key, or in the target's manifest."""
    from app.core.capture_naming import files_carrying, manifest_capture_numbers
    from app.core.paths import METADATA_DIR
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    names = [r.new_name.lower() for r in plan.renames]
    paths = [str(p) for r in plan.renames for p in (r.new_path, r.new_preview_path, r.new_raw_buffer_path) if p]
    keys = [k for r in plan.renames for k in r.new_keys.values()]
    held_names, held_paths, held_keys = {}, {}, set()
    for start in range(0, max(len(names), len(paths)), 500):
        for image_id, name, fp in (db.query(RecordImage.id, RecordImage.filename, RecordImage.file_path)
                                   .filter(func.lower(RecordImage.filename).in_(names[start:start + 500])
                                           | RecordImage.file_path.in_(paths[start:start + 500]))):
            held_names.setdefault(name.lower(), image_id)
            held_paths.setdefault(fp, image_id)
    for start in range(0, len(keys), 500):
        held_keys.update(k for (k,) in db.query(OffsiteObject.object_key)
                         .filter(OffsiteObject.object_key.in_(keys[start:start + 500])))
    in_manifest = manifest_capture_numbers(tgt_root / METADATA_DIR / MANIFEST_NAME, tgt_stem)

    seen = {}
    for r in plan.renames:
        found = list(files_carrying(tgt_captures, Path(r.new_name).stem))
        if r.new_thumbnail_path is not None and r.new_thumbnail_path != r.old_thumbnail_path \
                and r.new_thumbnail_path.exists():
            found.append(r.new_thumbnail_path)
        for p in found:
            plan.refuse("new_name_collides", {"image_id": r.image_id, "with": "file", "path": str(p)})
        holder = held_names.get(r.new_name.lower())
        if holder is None:
            holder = next((held_paths[str(p)] for p in (r.new_path, r.new_preview_path, r.new_raw_buffer_path)
                           if p is not None and str(p) in held_paths), None)
        if holder is not None:
            plan.refuse("new_name_collides", {"image_id": r.image_id, "with": "record_image",
                                              "record_image_id": holder})
        for key in r.new_keys.values():
            if key in held_keys:
                plan.refuse("new_name_collides", {"image_id": r.image_id, "with": "offsite_key", "key": key})
        if r.number in in_manifest:
            plan.refuse("new_name_collides", {"image_id": r.image_id, "with": "target_manifest", "number": r.number})
        if r.new_name.lower() in seen:
            plan.refuse("new_name_collides", {"image_id": r.image_id, "with": "plan",
                                              "record_image_id": seen[r.new_name.lower()]})
        seen.setdefault(r.new_name.lower(), r.image_id)
