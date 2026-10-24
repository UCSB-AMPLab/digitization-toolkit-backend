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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.core import config
from app.core.paths import OFFSITE_STAGING_DIR, RAW_SUFFIXES, is_raw_suffix, preview_sidecar_of, resolve_within_storage
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
    """Yield original image files under root, skipping derived thumbnails and the offsite upload's staged links."""
    for p in root.rglob("*"):
        if "thumbnails" in p.parts:
            continue
        if p.relative_to(root).parts[:1] == (OFFSITE_STAGING_DIR,):
            continue
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            yield p


@dataclass
class FixityIndex:
    """The capture-time checksums the manifests hold, and how to find an image's.

    by_path and by_name: capture_id -> {absolute path or file name: sha256},
    from the capture records and from relocation records with no image id.
    by_image: (capture_id, image id) -> sha256, from relocation records that
    name their image; None when two such records disagree. captured: the
    capture ids that have a capture record in some manifest.
    """
    by_path: Dict[str, Dict[str, str]] = field(default_factory=dict)
    by_name: Dict[str, Dict[str, str]] = field(default_factory=dict)
    by_image: Dict[Tuple[str, int], Optional[str]] = field(default_factory=dict)
    captured: Set[str] = field(default_factory=set)
    # Absolute paths of RAW previews the manifests give a digest for: a
    # capture record's `<role>_preview` entries and `preview_digest` records.
    previews: Set[str] = field(default_factory=set)

    def expected(self, capture_id: Optional[str], image_id: Optional[int], resolved: Path) -> Optional[str]:
        """The sha256 an image's file should have, or None if none can be resolved.

        A relocation record naming the image decides first: it needs no chain
        and no order, and a name reused by a later renumber cannot mislead it.
        Two such records that disagree resolve to None. Otherwise the file's
        resolved path, then its name, under its capture id.
        """
        if not capture_id:
            return None
        key = (capture_id, image_id)
        if image_id is not None and key in self.by_image:
            return self.by_image[key]
        paths = self.by_path.get(capture_id)
        expected = paths.get(str(resolved)) if paths else None
        if expected is None:
            names = self.by_name.get(capture_id)
            expected = names.get(resolved.name) if names else None
        return expected


def read_manifest_lines(path: Path) -> Tuple[List[str], int]:
    """A manifest's lines, decoded one by one, and how many could not be decoded.

    The file is split on newline bytes and each line decoded as UTF-8 on
    its own, so a line a power cut cut inside a multibyte character (copies
    are written with ensure_ascii=False) is skipped, and counted, while the
    lines around it are read. Lines are returned without their newline.
    Raises OSError when the file cannot be read.
    """
    lines: List[str] = []
    undecodable = 0
    for raw in path.read_bytes().split(b"\n"):
        try:
            lines.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            undecodable += 1
    return lines, undecodable


def build_fixity_index(projects_dir: Path) -> FixityIndex:
    """Index every metadata/manifest.jsonl under `projects_dir`: each project's, and each stemmed volume's.

    A capture record's paths are relative to the directory that holds its
    `metadata/`: the project's directory, or the volume's `<stem>/`.

    An upload record (capture.manifestHandler.append_upload_record) has the
    shape of a capture record and is read as one: every upload carries a
    capture id, the client's or one of its own.

    Capture records give each file's sha256 by resolved path and by name (the
    name map is a fallback for when a stored absolute path differs from the
    manifest's, e.g. after an SD re-clone remounts the tree elsewhere).

    A relocation record (capture.manifestHandler.append_relocation_record)
    says a captured file was renamed. One that carries `image_id` and
    `sha256` (every record the appliance writes) gives that image its
    checksum directly; no order between records matters. One without
    `image_id` (written by hand, or in an older format) is followed as a
    chain: its `to` path and name get the sha256 its `from` path or name has.
    Those are applied in line order within each manifest; a chain whose
    links sit in different manifests is completed by sweeping the manifests
    again, up to once per manifest, until a sweep changes nothing.
    `relocated_at` is information only and is never used for ordering,
    because a unit without a real-time clock can stamp a later rename with
    an earlier time.

    A RAW preview's digest comes from either source: its `<role>_preview`
    entry in the capture record (written at capture), or a `preview_digest`
    record (capture.manifestHandler.PREVIEW_DIGEST_RECORD_TYPE, appended by
    the backfill). Both are read into by_path and by_name under the capture
    id, so FixityIndex.expected finds a preview with image_id None. A
    `preview_digest` record is not a capture record: its capture id is not
    added to `captured`, and it never enters by_image, which holds masters.
    Each preview's path goes into `previews`.
    """
    from capture.manifestHandler import PREVIEW_DIGEST_RECORD_TYPE
    index = FixityIndex()
    if not projects_dir.exists():
        return index
    chains: List[List[Tuple[str, str, str]]] = []  # per manifest, in line order: (capture_id, from abs, to abs)

    for manifest in _manifests(projects_dir):
        project_root = manifest_base(manifest, projects_dir)
        try:
            lines, unparsed = read_manifest_lines(manifest)
        except OSError:
            continue
        chain: List[Tuple[str, str, str]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # A line a power cut tore: the appends start a new line after it.
                unparsed += 1
                continue
            if not isinstance(rec, dict):
                continue
            cid = rec.get("capture_id")
            if not cid:
                continue
            if rec.get("record_type") == "relocation":
                image_id, sha = rec.get("image_id"), rec.get("sha256")
                if isinstance(image_id, int) and sha:
                    key = (cid, image_id)
                    if key in index.by_image and index.by_image[key] != sha:
                        logger.warning(f"Relocation records of image {image_id} (capture {cid}) disagree on its sha256")
                        index.by_image[key] = None
                    elif key not in index.by_image:
                        index.by_image[key] = sha
                    continue
                frm, to = rec.get("from"), rec.get("to")
                if frm and to:
                    try:
                        chain.append((cid, str((project_root / frm).resolve()), str((project_root / to).resolve())))
                    except OSError:
                        pass
                continue
            backfilled = rec.get("record_type") == PREVIEW_DIGEST_RECORD_TYPE
            if not backfilled:
                index.captured.add(cid)
            for f in rec.get("files", []) or []:
                if not isinstance(f, dict):
                    continue
                sha = f.get("sha256")
                rel = f.get("relative_path")
                if not sha or not rel:
                    continue
                try:
                    abs_path = str((project_root / rel).resolve())
                except OSError:
                    continue
                index.by_path.setdefault(cid, {})[abs_path] = sha
                index.by_name.setdefault(cid, {})[Path(rel).name] = sha
                if backfilled or str(f.get("role", "")).endswith("_preview"):
                    index.previews.add(abs_path)
        if unparsed:
            logger.warning(f"Capture manifest {manifest}: skipped {unparsed} line(s) that are not JSON or not "
                           "UTF-8 (torn writes)")
        if chain:
            chains.append(chain)

    for _ in range(len(chains)):
        changed = False
        for chain in chains:
            for cid, frm, to in chain:
                sha = index.by_path.get(cid, {}).get(frm) or index.by_name.get(cid, {}).get(Path(frm).name)
                if not sha:
                    continue
                if index.by_path.get(cid, {}).get(to) != sha or index.by_name.get(cid, {}).get(Path(to).name) != sha:
                    index.by_path.setdefault(cid, {})[to] = sha
                    index.by_name.setdefault(cid, {})[Path(to).name] = sha
                    changed = True
        if not changed:
            break
    return index


RETIRED_MANIFESTS_DIR = ".retired-manifests"
# `<project dir>-<UTC timestamp>[-<n>]-manifest.jsonl` (storage_ops.retire_project_manifest)
_RETIRED_NAME = re.compile(r"^(?P<dir>.+)-\d{8}T\d{6}Z(-\d+)?-manifest\.jsonl$")


def _manifests(projects_dir: Path) -> List[Path]:
    """Every capture manifest under `projects_dir`.

    Each project's and each stemmed volume's metadata/manifest.jsonl, and the
    manifests of deleted projects kept in `.retired-manifests/`.
    """
    if not projects_dir.exists():
        return []
    found = list(projects_dir.glob("**/metadata/manifest.jsonl"))
    found.extend(p for p in projects_dir.glob(f"{RETIRED_MANIFESTS_DIR}/*-manifest.jsonl")
                 if _RETIRED_NAME.match(p.name))
    return found


def manifest_base(manifest: Path, projects_dir: Path) -> Path:
    """The directory a manifest's paths are relative to.

    The directory holding its `metadata/`: a project's directory or a
    volume's `<stem>/`. A retired manifest's paths stay relative to the
    project directory it was taken from, `<projects root>/<project dir>`,
    which its name carries.
    """
    if manifest.parent.name == RETIRED_MANIFESTS_DIR:
        m = _RETIRED_NAME.match(manifest.name)
        if m:
            return projects_dir / m.group("dir")
    return manifest.parent.parent


def capture_record_lines(projects_dir: Path, capture_ids: Set[str]) -> List[str]:
    """The capture records of `capture_ids`, as their manifest lines, one per capture.

    Read from every manifest build_fixity_index reads. A volume captured
    before it had a stem has its earlier capture records in its project's
    manifest (or a retired one) and its later ones in its own, so all are
    read. The startup migration copies a volume's records into its own
    manifest, marked `copied_from`; where the capture-time record is still
    there, it is the one returned, and a copy only for a capture whose
    original no manifest holds. An upload record with one of the capture ids
    is returned as a capture record is. Relocation and preview_digest records
    are left out: they are the appliance's own bookkeeping, of files renamed
    after capture and of preview digests computed after it. Manifests are
    read in path order and each in line order. Unreadable manifests and
    lines that are not JSON objects are skipped.
    """
    from capture.manifestHandler import is_capture_record

    originals: List[Tuple[str, str]] = []
    copies: List[Tuple[str, str]] = []
    for manifest in sorted(_manifests(projects_dir)):
        try:
            lines, _ = read_manifest_lines(manifest)
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if (is_capture_record(entry) and entry.get("capture_id") in capture_ids):
                (copies if "copied_from" in entry else originals).append((entry["capture_id"], stripped))
    kept: List[str] = []
    kept_lines: Set[str] = set()
    seen: Set[str] = set()
    for cid, line in originals:
        if line not in kept_lines:
            kept_lines.add(line)
            kept.append(line)
        seen.add(cid)
    for cid, line in copies:
        if cid not in seen:
            kept.append(line)
            seen.add(cid)
    return kept


def _build_manifest_index(projects_dir: Path) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    """(by_path, by_name) of build_fixity_index: capture_id -> {absolute path: sha256}, {file name: sha256}."""
    index = build_fixity_index(projects_dir)
    return index.by_path, index.by_name


def verify_images_against_manifest(images) -> List[dict]:
    """Re-hash each image and compare it to its capture-time manifest sha256.

    Returns the images that fail: each entry has "reason" "mismatch" (the
    bytes on disk differ from what was recorded at capture, with both
    sha256s) or "unresolved" (the image's capture id has a capture record in
    some manifest, but no checksum can be resolved for this image: a master
    renamed whose relocation record was not written, say). An image with no
    capture id, or whose capture id has no capture record in any manifest,
    is skipped, as there is no capture-time fixity to check: an upload made
    before uploads were written to the manifest is one. A later upload is
    checked against its upload record. A missing file is skipped too; it is
    a separate failure the caller handles. So is a master freed from the
    card (app.core.offsite_free): its bytes are in the bucket, verified by
    SHA1 when it was freed. Read-only.
    """
    index = build_fixity_index(config.settings.projects_dir.resolve())
    mismatches: List[dict] = []
    for img in images:
        if not img.capture_id or getattr(img, "master_freed_at", None) is not None:
            continue
        resolved = resolve_within_storage(img.file_path)
        if resolved is None or not resolved.exists():
            continue
        expected = index.expected(img.capture_id, img.id, resolved)
        if expected is None:
            if img.capture_id in index.captured:
                mismatches.append({
                    "reason": "unresolved",
                    "record_image_id": img.id,
                    "record_id": img.record_id,
                    "capture_id": img.capture_id,
                    "file_path": img.file_path,
                })
            continue
        try:
            actual = _sha256(resolved)
        except OSError:
            continue
        if actual != expected:
            mismatches.append({
                "reason": "mismatch",
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
    freed_files: List[dict] = []
    missing_thumbnails: List[dict] = []
    # A RAW preview kept on the card after its master was freed (a hand
    # clearance of the masters only) has its master's name to go by.
    kept_previews: set = set()

    for img in images:
        resolved = resolve_within_storage(img.file_path)
        if img.master_freed_at is not None:
            # Stored offsite and freed from the card on purpose: not missing (NEH-248).
            freed_files.append({
                "record_image_id": img.id,
                "record_id": img.record_id,
                "file_path": img.file_path,
                "freed_at": img.master_freed_at.isoformat(),
            })
            if resolved is not None and is_raw_suffix(resolved) and img.preview_freed_at is None:
                kept_previews.add(str(preview_sidecar_of(resolved)))
            if resolved is not None and resolved.exists():
                referenced.add(str(resolved))  # still on the card: its row's, not an orphan
        elif resolved is None or not resolved.exists():
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
    index = build_fixity_index(config.settings.projects_dir.resolve())
    manifest_paths: set = set()
    for paths in index.by_path.values():
        manifest_paths.update(paths.keys())
    # A preview's manifest entry does not make it known: the manifest keeps
    # the lines of deleted captures, so a preview counts as known only
    # through its master, below.
    manifest_paths -= index.previews
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
    known |= kept_previews

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
            if not img.capture_id or img.master_freed_at is not None:
                continue
            resolved = resolve_within_storage(img.file_path)
            if resolved is None or not resolved.exists():
                continue  # already reported under missing_files
            expected = index.expected(img.capture_id, img.id, resolved)
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
        "freed_files": len(freed_files),
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
        "freed_files": freed_files,
        "missing_thumbnails": missing_thumbnails,
        "orphan_files": orphan_files,
        "manifest_mismatches": manifest_mismatches,
        "manifest_missing_entry": manifest_missing_entry,
        "orphan_records": orphan_records,
    }
