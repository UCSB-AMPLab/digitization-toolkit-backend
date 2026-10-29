"""Rebuild a volume from paired JPGs captured outside Captua, whose masters are already in the bucket.

    python -m app.core.rebuild_volume <stem> <bundle dir> [--apply]

For volumes digitised before Captua with two cameras, one per page of each
opening: within each delivery, image 2k-1 is the right-hand page and image 2k
the left-hand page of one opening, taken by one press. The masters were
copied server-side into the bucket under contract names, at
`<tenant>/<stem>/captures/<stem>-img_<nnnn>_c<n>.jpg`, and never come to the
card. This command gives an empty volume the records it would have had if
Captua had captured those openings, one dual record per pair, as
app.api.cameras.trigger_dual_capture writes one, and a single record for an
image left without a partner, with each master stored offsite and freed from
birth, so the volume can be handed over, reviewed in Fisqua and its rejected
pages retaken from the unit like any other.

The bundle is a directory with

* `plan.json`: `{"stem", "counterAfter", "records": [...]}`, each record with
  `ordinal` (its position and its file name index; its sequence),
  `captureId`, `pairId`, `mode` (`dual` or `single`), `capturedAt`,
  `capturedAtSource` and `images`, each image with `volumeOrder`,
  `filename`, `role` and `rotateDeg`. It decides records, names, ids, dates,
  roles and rotation. A dual record has one `left` image named
  `<stem>-img_<nnnn>_c0.jpg` (camera 0, the even image) and one `right`
  image named `..._c1.jpg` (camera 1, the odd image), and its filename
  index `<stem>-img_<nnnn>` as its pair id, as capture gives a dual capture
  one (capture.service.dual_capture_image); a single record has one
  `single` image named `..._c0.jpg`, and a null pair id.
* `images.jsonl`: one line per source image, with `volumeOrder`,
  `sourceKey`, `bytes`, `sha256`, `sha1`, `width`, `height`, `format`, the
  EXIF facts and `thumbnail`, a path relative to the bundle to a ready 600px
  thumbnail, made upright (from the pixels rotated by the plan's rotateDeg)
  as app.core.thumbnail makes one, and copied as it is. It decides bytes,
  digests, size and thumbnails.

`rotateDeg` is authoritative: a camera pointing straight down cannot tell
which way is up, so the EXIF Orientation the cameras wrote is close to
random, and each camera has one fixed rotation, verified by eye. It means,
as everywhere in Captua (capture.camera.recorded_rotation), the clockwise
degrees that display the image upright from the pixels as the camera wrote
them. The EXIF Orientation is recorded as provenance only.

The bundle is refused when the two files do not name the same images; when a
volumeOrder, a file name, a capture id, a pair id or an ordinal repeats; when
a dual record does not have exactly one `left` image named `_c0` and one
`right` image named `_c1` for its ordinal, or a pair id other than its
filename index (a UUID included); when a single record does not have exactly
one `single` image named `_c0`, or has a pair id; when a rotateDeg is not 0, 90, 180 or 270; when the ordinals are not 1
to N; or when a sourceKey repeats. The plan shape of 0.1, one entry per
image, is not read: no unit ran it but the bench.

Nothing is written unless every precondition holds:

* the volume with the stem exists, is in a project, and has no records, no
  images and a capture counter of 0. A rerun finds the counter an earlier
  run raised: equal to counterAfter, with records that run made, each
  whole, and nothing else; or above it, when an operator has captured into
  the volume since, and then each record not of the bundle must carry
  numbers above counterAfter (a counter above 0 and below counterAfter is
  refused);
* offsite is configured (a remote and a valid tenant);
* a fresh listing of `<tenant>/<stem>/captures/` (a listing: the unit's key
  cannot read) shows every planned key, both files of each pair, with the
  planned SHA1 and size;
* `counterAfter` is at least the number of records;
* no planned master is on the card, no other offsite row holds a planned
  key, no image elsewhere carries a planned capture id, and the volume's
  manifest holds no capture but this bundle's and those numbered above
  counterAfter.

An applied run holds an flock on `<stem>/metadata/.rebuild.lock` from its
checks to its end, and refuses to start while another run holds it.

With `--apply`, the counter is first raised to counterAfter
(app.core.capture_counter.raise_capture_counter), so a capture into the
volume during the run takes counterAfter + 1 and never a planned number. A
capture that landed between the checks and the raise has taken a planned
number; the counter the raise found then differs from the one the checks
read, and the run stops before any record is written. Then each record, in
ordinal order, is written whole or not at all:

1. its images' thumbnails are copied to `<stem>/thumbnails/<uuid hex>_thumb.jpg`;
2. one capture line is appended to `<stem>/metadata/manifest.jsonl`, of the
   shape capture writes (capture.manifestHandler.CaptureRecord), with the
   record's capture id and pair id, one file per image (its role, path,
   bytes, sha256 and rotation), `cameras: []`, `timestamp_utc` from
   capturedAt and a `provenance` object: `rebuiltFrom`, naming for each
   file the source object and its EXIF Orientation, and `capturedAtSource`;
   a rerun does not append a line a capture id already has;
3. in one transaction, the record (dual or single, captured, sequence =
   ordinal), its images (the canonical paths of the absent masters, each
   master freed with the listed size, the shared capture id and pair id,
   each image's role and rotation) and a verified offsite row per image at
   the planned key with the planned SHA1, committed through
   app.core.image_upload._claim, the path adopt-map writes its rows by;
4. after that commit, one line per image in
   `<stem>/metadata/offsite-freed-<UTC>.jsonl`, as mark-freed writes it,
   with `method: "rebuild"`.

A failure before the commit removes the record's thumbnails and rolls the
transaction back; the manifest line stays, as a capture's does, since the
manifest is append-only. Then any record not of the bundle (a capture
taken during the run, whose sequence a later record may have taken too) is
moved after the last record, keeping its order (_resequence_others), and one
system log event, `volume_rebuilt`, records the counts. Each record is left
`captured`: the operator hands the volume over.

A rerun skips each record whose capture id already has images in the
volume, once they agree with the plan and the bundle in every field
_differences compares, writes the freed lines an interruption lost, and
copies a thumbnail again from the bundle if its file is missing.

The operator must not promote (hand over) the volume, nor free or restore
it, until a run reports it complete: its images reach Fisqua, and its freed
state the card, only as the run writes them.

Locks: each record's transaction takes the volume's row (locked_volume_query),
as a capture does before it adds a record, and then, in _claim, the row of
the record it has just inserted. No other transaction can see that record
before the commit, so no other path can hold or wait on its lock, and the
record-before-volume order of paths that lock existing records is not
crossed. No database transaction stays open across the listing or a file copy.

Version: 0.2.0
"""

import argparse
import json
import logging
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.catalogue_backup import VerificationUnavailable
from app.core.paths import CAPTURES_DIR, METADATA_DIR, THUMBNAILS_DIR

logger = logging.getLogger(__name__)

CATEGORY = "system"
REBUILT_ACTION = "volume_rebuilt"
FREED_METHOD = "rebuild"
SOURCE_BUCKET = "zasqua-transition"
ROTATIONS = (0, 90, 180, 270)
MODES = ("dual", "single")
# The roles of a record's images, in the order they are written, and the
# camera each file name carries: as a dual capture with left_camera_index 0
# names them (app.api.cameras.trigger_dual_capture), and a single one.
ROLES = {"dual": ("left", "right"), "single": ("single",)}
CAMERA_FOR_ROLE = {"left": 0, "right": 1, "single": 0}
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RebuildRefused(ValueError):
    """A precondition failed; nothing was written. `problems` names each record or image concerned."""

    def __init__(self, message: str, problems: Optional[List[dict]] = None):
        super().__init__(message)
        self.problems = problems or []


class RebuildFailed(RuntimeError):
    """A record could not be written; it was rolled back, and the run stopped."""


@dataclass
class PlannedImage:
    volume_order: int
    filename: str
    role: str
    rotate_deg: int
    source_key: str
    size: int
    sha256: str
    sha1: str
    width: int
    height: int
    thumbnail: Path
    exif_orientation: object  # as images.jsonl gives it (None when absent): provenance only


@dataclass
class Planned:
    ordinal: int
    capture_id: str
    pair_id: Optional[str]
    mode: str
    captured_at: datetime  # aware, UTC
    captured_at_source: Optional[str]
    images: List[PlannedImage] = field(default_factory=list)  # in ROLES order


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RebuildRefused(f"cannot read {path}: {exc.strerror or exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RebuildRefused(f"{path} is not JSON: {exc}") from exc


def _read_jsonl(path: Path) -> list:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RebuildRefused(f"cannot read {path}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise RebuildRefused(f"{path} is not UTF-8: {exc}") from exc
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RebuildRefused(f"line {n} of {path} is not JSON: {exc}") from exc
    return out


def _captured_at(value) -> Optional[datetime]:
    """capturedAt as an aware UTC datetime, or None when it is not an ISO 8601 time with an offset."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _uuid(value) -> Optional[str]:
    """`value` when it is a UUID in canonical form, else None."""
    try:
        canonical = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None
    return canonical if canonical == value else None


def _repeats(entries: list, field: str, key: str = "volumeOrder") -> List[dict]:
    seen: Dict[object, int] = {}
    for e in entries:
        if e[field] is not None:
            seen[e[field]] = seen.get(e[field], 0) + 1
    return [{key: e[key], "field": field, "value": e[field], "reason": "repeated"}
            for e in entries if e[field] is not None and seen[e[field]] > 1]


def _record_problem(stem: str, ordinal: int, e: dict) -> Optional[dict]:
    """The first reason a plan record (with a valid ordinal) is refused, or None; the record parsed into `e`."""
    from app.core.capture_naming import capture_filename_index

    base = {"ordinal": ordinal}
    index = capture_filename_index(stem, ordinal)
    mode, capture_id, pair_id = e.get("mode"), e.get("captureId"), e.get("pairId")
    if mode not in MODES:
        return {**base, "reason": "mode_invalid", "value": mode}
    if _uuid(capture_id) is None:
        return {**base, "reason": "capture_id_invalid", "value": capture_id}
    # A dual capture's pair id is its filename index (capture.service.dual_capture_image).
    wanted_pair = index if mode == "dual" else None
    if pair_id != wanted_pair or (mode == "dual" and not isinstance(pair_id, str)):
        return {**base, "reason": "pair_id_invalid", "value": pair_id, "expected": wanted_pair}
    if _captured_at(e.get("capturedAt")) is None:
        return {**base, "reason": "captured_at_invalid", "value": e.get("capturedAt")}
    images = e.get("images")
    if not isinstance(images, list) or not all(isinstance(i, dict) for i in images):
        return {**base, "reason": "malformed_images"}
    for img in images:
        if not _is_int(img.get("volumeOrder")) or img["volumeOrder"] < 1:
            return {**base, "reason": "volume_order_invalid", "value": img.get("volumeOrder")}
    roles = sorted(str(img.get("role")) for img in images)
    if roles != sorted(ROLES[mode]):
        return {**base, "reason": f"{mode}_{'roles' if mode == 'dual' else 'images'}_invalid", "roles": roles}
    for img in images:
        wanted = f"{index}_c{CAMERA_FOR_ROLE[img['role']]}.jpg"
        if img.get("filename") != wanted:
            return {**base, "volumeOrder": img["volumeOrder"], "reason": "filename_not_its_ordinal",
                    "value": img.get("filename"), "expected": wanted}
    for img in images:
        rotate = img.get("rotateDeg")
        if not _is_int(rotate) or rotate not in ROTATIONS:
            return {**base, "volumeOrder": img["volumeOrder"], "reason": "rotate_deg_invalid", "value": rotate}
    return None


def load_bundle(bundle_dir: str, stem: str) -> tuple:
    """(counterAfter, [Planned] in ordinal order) from a bundle. Raises RebuildRefused."""
    from app.core.capture_naming import MAX_CAPTURE_NUMBER

    bundle = Path(bundle_dir)
    if not bundle.is_dir():
        raise RebuildRefused(f"{bundle} is not a directory")
    try:
        bundle_root = bundle.resolve()
    except OSError as exc:
        raise RebuildRefused(f"cannot resolve {bundle}: {exc}") from exc
    plan = _read_json(bundle / "plan.json")
    if not isinstance(plan, dict):
        raise RebuildRefused("plan.json is not a JSON object")
    if plan.get("stem") != stem:
        raise RebuildRefused(f"plan.json is for the stem {plan.get('stem')!r}, not {stem!r}")
    counter_after = plan.get("counterAfter")
    if not _is_int(counter_after) or not 0 <= counter_after <= MAX_CAPTURE_NUMBER:
        raise RebuildRefused(f"plan.json's counterAfter {counter_after!r} is not a whole number "
                             f"from 0 to {MAX_CAPTURE_NUMBER}")
    entries = plan.get("records")
    if not isinstance(entries, list) or not entries:
        raise RebuildRefused("plan.json has no records (the plan shape of 0.1, one entry per image, "
                             "is not read)")

    problems = []
    records, images = [], []
    for index, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append({"index": index, "reason": "malformed_plan_record"})
            continue
        ordinal = e.get("ordinal")
        if not _is_int(ordinal) or not 1 <= ordinal <= MAX_CAPTURE_NUMBER:
            problems.append({"index": index, "reason": "ordinal_invalid", "value": ordinal})
            continue
        problem = _record_problem(stem, ordinal, e)
        if problem is not None:
            problems.append(problem)
            continue
        source = e.get("capturedAtSource")
        records.append({"ordinal": ordinal, "captureId": e["captureId"], "pairId": e.get("pairId"),
                        "mode": e["mode"], "at": _captured_at(e["capturedAt"]),
                        "source": source if isinstance(source, str) else None})
        images.extend({"ordinal": ordinal, "volumeOrder": i["volumeOrder"], "filename": i["filename"],
                       "role": i["role"], "rotate": i["rotateDeg"]} for i in e["images"])
    if problems:
        reasons = ", ".join(sorted({p["reason"] for p in problems}))
        raise RebuildRefused(f"plan.json has {len(problems)} invalid record(s) ({reasons})", problems)
    for name in ("ordinal", "captureId", "pairId"):
        problems.extend(_repeats(records, name, key="ordinal"))
    for name in ("volumeOrder", "filename"):
        problems.extend(_repeats(images, name))
    if problems:
        fields = sorted({p["field"] for p in problems})
        raise RebuildRefused(f"plan.json repeats a {', '.join(fields)} across records; each must be unique",
                             problems)
    ordinals = sorted(r["ordinal"] for r in records)
    if ordinals != list(range(1, len(records) + 1)):
        missing = sorted(set(range(1, len(records) + 1)) - set(ordinals))
        raise RebuildRefused(f"plan.json's ordinals are not 1 to {len(records)}",
                             [{"ordinal": n, "reason": "ordinal_missing"} for n in missing])

    facts = {}
    for index, line in enumerate(_read_jsonl(bundle / "images.jsonl")):
        order = line.get("volumeOrder") if isinstance(line, dict) else None
        if not _is_int(order):
            problems.append({"line": index + 1, "reason": "malformed_image_line"})
            continue
        if order in facts:
            problems.append({"volumeOrder": order, "field": "volumeOrder", "reason": "repeated"})
            continue
        sha1 = line.get("sha1").lower() if isinstance(line.get("sha1"), str) else None
        sha256 = line.get("sha256").lower() if isinstance(line.get("sha256"), str) else None
        size, width, height = line.get("bytes"), line.get("width"), line.get("height")
        thumb_rel, source_key = line.get("thumbnail"), line.get("sourceKey")
        reason = None
        if not sha1 or not _SHA1.match(sha1):
            reason = "sha1_invalid"
        elif not sha256 or not _SHA256.match(sha256):
            reason = "sha256_invalid"
        elif not _is_int(size) or size < 0:
            reason = "bytes_invalid"
        elif not _is_int(width) or not _is_int(height) or width < 1 or height < 1:
            reason = "dimensions_invalid"
        elif line.get("format") != "JPEG":
            reason = "not_a_jpeg"
        elif not isinstance(source_key, str) or not source_key:
            reason = "source_key_missing"
        elif not isinstance(thumb_rel, str) or not thumb_rel or Path(thumb_rel).is_absolute():
            reason = "thumbnail_invalid"
        thumb = None
        if reason is None:
            try:
                thumb = (bundle_root / thumb_rel).resolve()
            except OSError:
                thumb = None
            if thumb is None or not thumb.is_relative_to(bundle_root) or not thumb.is_file():
                reason = "thumbnail_missing"
        if reason is not None:
            problems.append({"volumeOrder": order, "reason": reason})
            continue
        facts[order] = {"sha1": sha1, "sha256": sha256, "bytes": size, "width": width, "height": height,
                        "thumbnail": thumb, "sourceKey": source_key,
                        "orientation": line.get("exifOrientation")}
    if problems:
        raise RebuildRefused(f"images.jsonl has {len(problems)} invalid or repeated line(s)", problems)
    problems = _repeats([{"volumeOrder": o, "sourceKey": f["sourceKey"]} for o, f in sorted(facts.items())],
                        "sourceKey")
    if problems:
        raise RebuildRefused("images.jsonl repeats a sourceKey across images; each must be unique", problems)

    planned_orders = {i["volumeOrder"] for i in images}
    if planned_orders != set(facts):
        diff = ([{"volumeOrder": o, "reason": "only_in_plan"} for o in sorted(planned_orders - set(facts))]
                + [{"volumeOrder": o, "reason": "only_in_images"} for o in sorted(set(facts) - planned_orders)])
        raise RebuildRefused("plan.json and images.jsonl do not name the same images", diff)

    by_record: Dict[int, List[PlannedImage]] = {}
    for i in images:
        f = facts[i["volumeOrder"]]
        by_record.setdefault(i["ordinal"], []).append(PlannedImage(
            i["volumeOrder"], i["filename"], i["role"], i["rotate"], f["sourceKey"], f["bytes"], f["sha256"],
            f["sha1"], f["width"], f["height"], f["thumbnail"], f["orientation"]))
    out = []
    for r in sorted(records, key=lambda r: r["ordinal"]):
        order = ROLES[r["mode"]]
        out.append(Planned(r["ordinal"], r["captureId"], r["pairId"], r["mode"], r["at"], r["source"],
                           sorted(by_record[r["ordinal"]], key=lambda i: order.index(i.role))))
    return counter_after, out


def _all_images(planned: List[Planned]) -> List[tuple]:
    """(record, image) for every planned image, in ordinal order and each record's ROLES order."""
    return [(p, img) for p in planned for img in p.images]


# ---------------------------------------------------------------------------
# What is on the unit
# ---------------------------------------------------------------------------

def _manifest_captures(volume_dir: Path) -> Dict[str, dict]:
    """capture_id -> capture line in the volume's manifest.jsonl (a torn line is skipped)."""
    from app.core.integrity import read_manifest_lines
    from capture.manifestHandler import is_capture_record

    path = volume_dir / METADATA_DIR / "manifest.jsonl"
    if not path.exists():
        return {}
    try:
        lines, _ = read_manifest_lines(path)
    except OSError as exc:
        raise RebuildRefused(f"cannot read {path}: {exc}") from exc
    out = {}
    for line in lines:
        try:
            entry = json.loads(line) if line.strip() else None
        except json.JSONDecodeError:
            continue
        if is_capture_record(entry) and entry.get("capture_id"):
            out.setdefault(entry["capture_id"], entry)
    return out


def _freed_keys(volume_dir: Path) -> set:
    """The keys the volume's offsite-freed-*.jsonl manifests already have a line for."""
    keys = set()
    for path in (volume_dir / METADATA_DIR).glob("offsite-freed-*.jsonl"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                keys.add(entry["key"])
    return keys


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """A DateTime as naive UTC: the column gives naive values back, holding UTC as written."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)

def _differences(p: Planned, rec, imgs: list, rows: Dict[tuple, object], line: Optional[dict],
                 keys: Dict[str, str], paths: Dict[str, str]) -> List[str]:
    """The fields in which an earlier run's record, its images, offsite rows and manifest line differ from the plan.

    Empty when the record is whole and is what the plan and the bundle say it
    is: a record is never skipped as done on its capture id alone. An image's
    fields are named after its role (`left.rotate_deg`).
    """
    out = [name for name, ok in (
        ("capture_mode", rec.capture_mode == p.mode),
        ("status", rec.status == "captured"),
        ("sequence", rec.sequence == p.ordinal),
    ) if not ok]
    by_role = {img.role: img for img in imgs}
    if len(imgs) != len(p.images) or set(by_role) != {i.role for i in p.images}:
        return out + ["images"]
    for want in p.images:
        img = by_role[want.role]
        path = paths[want.filename]
        checks = [
            ("filename", img.filename == want.filename),
            ("file_path", img.file_path == path),
            ("capture_id", img.capture_id == p.capture_id),
            ("pair_id", img.pair_id == p.pair_id),
            ("rotate_deg", img.rotate_deg == want.rotate_deg),
            ("created_at", _naive_utc(img.created_at) == _naive_utc(p.captured_at)),
            ("file_size", img.file_size == want.size),
            ("resolution_width", img.resolution_width == want.width),
            ("resolution_height", img.resolution_height == want.height),
            ("master_freed_at", img.master_freed_at is not None),
            ("master_freed_bytes", img.master_freed_bytes == want.size),
            ("thumbnail_path", bool(img.thumbnail_path)
             and Path(img.thumbnail_path).parent == Path(path).parent.parent / THUMBNAILS_DIR),
        ]
        out.extend(f"{want.role}.{name}" for name, ok in checks if not ok)
        row = rows.get((img.id, "capture"))
        if row is None or row.verified_at is None:
            out.append(f"{want.role}.offsite.verified")
        else:
            if row.object_key != keys[want.filename]:
                out.append(f"{want.role}.offsite.key")
            if row.local_sha1 != want.sha1:
                out.append(f"{want.role}.offsite.local_sha1")
    out.extend(_line_differences(p, line))
    return out


def _line_differences(p: Planned, line: Optional[dict]) -> List[str]:
    """The fields in which a record's capture line differs from the plan and the bundle.

    Its pair id; each file entry (path, sha256, bytes, rotation), named
    after its role; its timestamp_utc against capturedAt in UTC; and its
    provenance (capturedAtSource, and each file's rebuiltFrom bucket,
    sourceKey and EXIF Orientation), each named on its own.
    """
    if line is None:
        return ["manifest.missing"]
    files = line.get("files")
    if (not isinstance(files, list) or len(files) != len(p.images) or not all(isinstance(f, dict) for f in files)
            or {f.get("role") for f in files} != {i.role for i in p.images}):
        return ["manifest.files"]
    by_role = {f["role"]: f for f in files}
    checks = [("manifest.pair_id", line.get("pair_id") == p.pair_id)]
    for want in p.images:
        f = by_role[want.role]
        checks += [
            (f"manifest.{want.role}.relative_path", f.get("relative_path") == f"{CAPTURES_DIR}/{want.filename}"),
            (f"manifest.{want.role}.sha256", f.get("sha256") == want.sha256),
            (f"manifest.{want.role}.bytes", f.get("bytes") == want.size),
            (f"manifest.{want.role}.rotate_deg", f.get("rotate_deg") == want.rotate_deg),
        ]
    # The line is reused, never appended again, so its time and its
    # sources must be the plan's and the bundle's too.
    checks.append(("manifest.timestamp_utc", _captured_at(line.get("timestamp_utc")) == p.captured_at))
    provenance = line.get("provenance") if isinstance(line.get("provenance"), dict) else {}
    checks.append(("manifest.provenance.capturedAtSource",
                   "capturedAtSource" in provenance and provenance["capturedAtSource"] == p.captured_at_source))
    sources = provenance.get("rebuiltFrom")
    sources = {s.get("role"): s for s in sources if isinstance(s, dict)} if isinstance(sources, list) else {}
    for want in p.images:
        s = sources.get(want.role, {})
        checks += [
            (f"manifest.provenance.{want.role}.bucket", s.get("bucket") == SOURCE_BUCKET),
            (f"manifest.provenance.{want.role}.key", s.get("key") == want.source_key),
            (f"manifest.provenance.{want.role}.exifOrientation",
             "exifOrientation" in s and s["exifOrientation"] == want.exif_orientation),
        ]
    return [name for name, ok in checks if not ok]


def _done(db: Session, collection_id: int, planned: List[Planned], keys: Dict[str, str],
          paths: Dict[str, str], in_manifest: Dict[str, dict]) -> tuple:
    """({capture_id: done record facts}, [other records], [differing records]) for the volume's records.

    A record whose images carry a planned capture id is an earlier run's,
    and is done only when it is whole and agrees with the plan and the
    bundle in every field _differences compares; otherwise it is listed,
    naming the fields, for the caller to refuse. A missing thumbnail file is
    not a difference: the applied run copies it again from the bundle. Every
    record whose images carry no planned capture id is returned among the
    others, with its images' names, for the caller to judge
    (_captures_since_raise).
    """
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage

    by_id = {p.capture_id: p for p in planned}
    records = db.query(Record).filter(Record.collection_id == collection_id).order_by(Record.id).all()
    images = (db.query(RecordImage).join(Record, RecordImage.record_id == Record.id)
              .filter(Record.collection_id == collection_id).all())
    by_record: Dict[int, list] = {}
    for img in images:
        by_record.setdefault(img.record_id, []).append(img)
    rows = {}
    for start in range(0, len(images), 500):
        chunk = [i.id for i in images[start:start + 500]]
        for row in db.query(OffsiteObject).filter(OffsiteObject.record_image_id.in_(chunk)):
            rows[(row.record_image_id, row.kind)] = row

    done, others, differ = {}, [], []
    for rec in records:
        imgs = by_record.get(rec.id, [])
        mine = [i for i in imgs if i.capture_id in by_id]
        if not mine:
            others.append({"record_id": rec.id, "reason": "not_this_bundles_work",
                           "images": [i.id for i in imgs], "filenames": [i.filename for i in imgs]})
            continue
        p = by_id[mine[0].capture_id]
        fields = _differences(p, rec, imgs, rows, in_manifest.get(p.capture_id), keys, paths)
        if p.capture_id in done:
            fields = ["capture_id_repeated"] + fields
        if fields:
            differ.append({"record_id": rec.id, "image_ids": sorted(i.id for i in imgs),
                           "capture_id": p.capture_id, "ordinal": p.ordinal, "reason": "differs_from_plan",
                           "fields": fields})
            continue
        done[p.capture_id] = {"record_id": rec.id, "images": {
            img.filename: {"image_id": img.id, "file_path": img.file_path, "freed_at": img.master_freed_at,
                           "freed_bytes": img.master_freed_bytes, "thumbnail_path": img.thumbnail_path,
                           "thumbnail_missing": not Path(img.thumbnail_path).is_file()}
            for img in imgs}}
    db.commit()
    return done, others, differ



def _capture_number(stem: str, name) -> Optional[int]:
    """The `<nnnn>` of a file named `<stem>-img_<nnnn>_c<n>.<ext>`, or None."""
    m = re.match(rf"^{re.escape(stem)}-img_(\d+)_c\d+\.[A-Za-z0-9]+$", name or "")
    return int(m.group(1)) if m else None


def _captures_since_raise(stem: str, others: List[dict], counter_after: int, counter: int) -> bool:
    """Whether every record not of this bundle is a capture taken after the run raised the counter.

    Such a record has images, carries no planned capture id, and each of its
    images is named with a number above counterAfter and at most the counter
    now. An operator may capture into the volume while it is rebuilt; since
    the counter is raised before the first record, those captures take
    counterAfter + 1 and on, and a rerun leaves them where they are.
    """
    for o in others:
        if not o["filenames"]:
            return False
        for name in o["filenames"]:
            n = _capture_number(stem, name)
            if n is None or not counter_after < n <= counter:
                return False
    return True


def _resequence_others(db: Session, collection_id: int, planned: List[Planned]) -> List[dict]:
    """Move the volume's records not of this bundle after its last record, keeping their order.

    A capture taken during the run took the sequence after the highest
    then in the volume, which a record rebuilt later in the run may also take
    (each record's sequence is its ordinal). Fisqua refuses a volume in
    which two records share a sequence, and a capture taken after the
    rebuilt records belongs after them. Only sequences change; no file is
    renamed, as in a renumber (app.core.record_sequence). The records are
    locked in id order, then the volume's row, through
    record_sequence.lock_volume_records, as every path that rewrites a
    volume's order does. Records already after the last rebuilt record, in
    distinct sequences, are left as they are.
    """
    from app.core.record_sequence import VolumeChangedError, lock_volume_records
    from app.models.record import RecordImage

    ids = {p.capture_id for p in planned}
    last = max(p.ordinal for p in planned)
    try:
        # The records in id order, then the volume's row, with the set read
        # again under the row: no record can join the volume until the commit.
        _, records = lock_volume_records(db, collection_id)
    except VolumeChangedError as exc:
        db.rollback()
        raise RebuildFailed("records kept being added to the volume while its order was being fixed; every "
                            "record is written, and a rerun of the same command fixes the order") from exc
    record_ids = [r.id for r in records]
    ours = set()
    for start in range(0, len(record_ids), 500):
        ours |= {rid for (rid,) in db.query(RecordImage.record_id).filter(
            RecordImage.record_id.in_(record_ids[start:start + 500]), RecordImage.capture_id.in_(ids))}
    others = [r for r in records if r.id not in ours]  # in listing order
    sequences = [r.sequence for r in others]
    if all(s is not None and s > last for s in sequences) and len(set(sequences)) == len(sequences):
        db.commit()
        return []
    moved = []
    for n, rec in enumerate(others, start=last + 1):
        if rec.sequence != n:
            moved.append({"record_id": rec.id, "from": rec.sequence, "to": n})
            rec.sequence = n
    db.commit()
    return moved


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _recopy_thumbnail(src: Path, dest: Path) -> None:
    """Copy a bundle thumbnail to the name its row already gives, flushed to disk."""
    part = dest.with_name(dest.name + ".part")
    try:
        shutil.copyfile(src, part)
        with open(part, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(part, dest)
        _fsync_dir(dest.parent)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _copy_thumbnail(src: Path, dest_dir: Path) -> Path:
    """Copy a bundle thumbnail to `dest_dir/<uuid4 hex>_thumb.jpg`, flushed to disk, as generate_thumbnail names one."""
    dest = dest_dir / f"{uuid.uuid4().hex}_thumb.jpg"
    try:
        shutil.copyfile(src, dest)
        with open(dest, "rb") as fh:
            os.fsync(fh.fileno())
        _fsync_dir(dest_dir)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest

def _capture_line(p: Planned, project_name: str) -> str:
    """The record's capture line: capture's own record shape, one file per image, no camera, the sources named."""
    from capture.manifestHandler import CaptureFile, CaptureRecord

    record = CaptureRecord(
        capture_id=p.capture_id,
        project_name=project_name,
        pair_id=p.pair_id,
        timestamp_utc=p.captured_at.isoformat(),
        files=[CaptureFile(role=img.role, relative_path=f"{CAPTURES_DIR}/{img.filename}", bytes=img.size,
                           mimetype="image/jpeg", sha256=img.sha256, rotate_deg=img.rotate_deg)
               for img in p.images],
        cameras=[],
    ).to_dict()
    record["provenance"] = {
        "rebuiltFrom": [{"role": img.role, "bucket": SOURCE_BUCKET, "key": img.source_key,
                         "exifOrientation": img.exif_orientation} for img in p.images],
        "capturedAtSource": p.captured_at_source,
    }
    return json.dumps(record, ensure_ascii=False)



def _freed_entry(image_id: int, record_id: int, file_path: str, key: str, sha1: str, size: int,
                 at: datetime) -> dict:
    """The freed manifest line, in mark-freed's shape (offsite_free._Object.entry), with method `rebuild`."""
    from app.core.offsite_free import _Object

    obj = _Object(image_id, record_id, "capture", file_path, Path(file_path), key, sha1)
    entry = obj.entry(bytes=size, sha1=sha1)
    # A DateTime column gives a naive value back (SQLite, and Postgres's
    # timestamp without time zone); it holds UTC, as written.
    entry["freed_at"] = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).isoformat()
    entry["method"] = FREED_METHOD
    return entry


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _offsite_tenant() -> str:
    from app.core import config
    from app.core.image_upload import ImageUploadMisconfigured, configured_tenant

    if not config.settings.OFFSITE_REMOTE.strip():
        raise RebuildRefused("offsite is not configured on this unit: CAPTUA_OFFSITE_REMOTE is empty")
    try:
        tenant = configured_tenant()
    except ImageUploadMisconfigured as exc:
        raise RebuildRefused(f"offsite is misconfigured: {exc}") from exc
    if tenant is None:
        raise RebuildRefused("offsite is not configured on this unit: CAPTUA_OFFSITE_TENANT is empty")
    return tenant

def _listing_problems(planned: List[Planned], keys: Dict[str, str]) -> tuple:
    """({key: listed size}, [problems]) from one fresh listing of every planned key, both files of each pair."""
    from app.core.offsite_free import _listings

    try:
        listed = _listings(keys[img.filename] for _, img in _all_images(planned))
    except VerificationUnavailable as exc:
        raise RebuildRefused(f"the bucket listing did not answer, so nothing was written: {exc}") from exc
    sizes, problems = {}, []
    for p, img in _all_images(planned):
        key = keys[img.filename]
        info = listed.get(key)
        base = {"ordinal": p.ordinal, "volumeOrder": img.volume_order, "key": key}
        if info is None or not info.present:
            problems.append({**base, "reason": "not_in_bucket"})
        elif info.sha1 is None:
            problems.append({**base, "reason": "listed_without_sha1"})
        elif info.sha1 != img.sha1:
            problems.append({**base, "reason": "bucket_sha1_differs", "listed_sha1": info.sha1,
                             "expected_sha1": img.sha1})
        elif info.size is None:
            problems.append({**base, "reason": "listed_without_size"})
        elif info.size != img.size:
            problems.append({**base, "reason": "bucket_size_differs", "listed_bytes": info.size,
                             "expected_bytes": img.size})
        else:
            sizes[key] = info.size
    return sizes, problems



LOCK_NAME = ".rebuild.lock"


class _volume_lock:
    """An exclusive flock on `<stem>/metadata/.rebuild.lock` for a whole applied run, or RebuildRefused.

    Two runs on one volume would each pass the checks against the other's
    partial work. The lock is advisory and per volume; the kernel releases
    it when the process ends, however it ends. The file is removed when the
    run ends; one a killed run left stays in `metadata/` until the next run
    takes and removes it.
    """

    def __init__(self, volume_dir: Path, stem: str):
        self.path = volume_dir / METADATA_DIR / LOCK_NAME
        self.stem = stem
        self.fd = None

    def __enter__(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RebuildRefused(f"another rebuild of volume {self.stem!r} is running "
                                     f"(it holds {self.path}); wait for it to finish")
            try:
                same = os.fstat(fd).st_ino == os.stat(self.path).st_ino
            except FileNotFoundError:
                same = False
            if same:  # not a file an ending run removed after this one opened it
                self.fd = fd
                return self
            os.close(fd)
        raise RebuildRefused(f"could not take the rebuild lock {self.path}")

    def __exit__(self, *exc):
        try:
            self.path.unlink(missing_ok=True)
        finally:
            os.close(self.fd)
        return False


def rebuild_volume(db: Session, stem: str, bundle_dir: str, *, apply: bool = False) -> dict:
    """Rebuild the empty volume `stem` from the bundle (module header). Raises RebuildRefused, RebuildFailed."""
    from app.core.storage_ops import resolve_project
    from app.models.collection import Collection
    from capture.project_manager import projects_root, volume_root

    stem = (stem or "").strip()
    collection = db.query(Collection).filter(Collection.filename_stem == stem).one_or_none() if stem else None
    if collection is None:
        db.rollback()
        raise RebuildRefused(f"no volume has the file name stem {stem!r}")
    db.commit()
    counter_after, planned = load_bundle(bundle_dir, stem)
    if counter_after < len(planned):
        raise RebuildRefused(f"plan.json's counterAfter {counter_after} is below its number of records "
                             f"{len(planned)}, its highest ordinal")
    tenant = _offsite_tenant()

    collection = db.get(Collection, collection.id)
    if collection is None or collection.filename_stem != stem:
        db.rollback()
        raise RebuildRefused(f"no volume has the file name stem {stem!r}")
    project = resolve_project(db, collection)
    if project is None:
        db.rollback()
        raise RebuildRefused(f"volume {stem!r} is not in a project, and a capture names its project")
    collection_id, project_name = collection.id, project.name
    db.commit()

    from app.core.storage_override import StorageUnavailableError

    try:
        root = projects_root()
    except StorageUnavailableError as exc:
        raise RebuildRefused(str(exc)) from exc
    volume_dir = volume_root(stem, root)
    args = dict(stem=stem, bundle_dir=bundle_dir, counter_after=counter_after, planned=planned, tenant=tenant,
                collection_id=collection_id, project_name=project_name, root=root, volume_dir=volume_dir)
    if not apply:
        return _run(db, apply=False, **args)
    with _volume_lock(volume_dir, stem):
        return _run(db, apply=True, **args)

def _run(db: Session, *, apply: bool, stem: str, bundle_dir: str, counter_after: int, planned: List[Planned],
         tenant: str, collection_id: int, project_name: str, root: Path, volume_dir: Path) -> dict:
    """The checks on the unit, then (with `apply`) the writes; under _volume_lock when applied."""
    from app.core.image_upload import capture_key
    from app.models.collection import Collection
    from app.models.offsite_object import OffsiteObject
    from app.models.record import RecordImage

    # Read again here: an applied run reads it under the volume's lock file.
    counter_before = db.query(Collection.capture_counter).filter(Collection.id == collection_id).scalar()
    db.commit()
    keys = {img.filename: capture_key(tenant, stem, img.filename) for _, img in _all_images(planned)}
    paths = {img.filename: str(volume_dir / CAPTURES_DIR / img.filename) for _, img in _all_images(planned)}

    # The counter is raised to counterAfter before the first record, so it is
    # 0 before any run, counterAfter after one, or above it by the captures
    # an operator has taken into the volume since.
    in_manifest = _manifest_captures(volume_dir)
    done, others, differ = _done(db, collection_id, planned, keys, paths, in_manifest)
    if differ:
        raise RebuildRefused(f"{len(differ)} record(s) an earlier run wrote differ from the plan and the bundle; "
                             "a record is never skipped as done unless it agrees with them", differ)
    if counter_before == 0:
        if others or done:
            raise RebuildRefused(f"volume {stem!r} already has records this bundle did not make",
                                 others or [{"reason": "records_with_counter_0", "records": len(done)}])
    elif counter_before == counter_after:
        if others:
            raise RebuildRefused(f"volume {stem!r} already has records this bundle did not make", others)
    elif counter_before > counter_after:
        # Captures taken since the raise; the manifest check below holds
        # every unplanned capture line to a number above counterAfter too.
        if others and not _captures_since_raise(stem, others, counter_after, counter_before):
            raise RebuildRefused(f"volume {stem!r} already has records this bundle did not make", others)
    else:
        raise RebuildRefused(f"the capture counter of volume {stem!r} is {counter_before}; a rebuild needs a "
                             f"volume that has never captured, or one an earlier run raised to {counter_after}",
                             others)

    todo = [p for p in planned if p.capture_id not in done]
    todo_images = _all_images(todo)
    on_card = [{"ordinal": p.ordinal, "volumeOrder": img.volume_order, "path": paths[img.filename],
                "reason": "master_on_card"} for p, img in todo_images if Path(paths[img.filename]).exists()]
    if on_card:
        raise RebuildRefused("a planned master is already on the card", on_card)

    held = {}
    todo_keys = [keys[img.filename] for _, img in todo_images]
    for start in range(0, len(todo_keys), 500):
        for row in db.query(OffsiteObject).filter(OffsiteObject.object_key.in_(todo_keys[start:start + 500])):
            held[row.object_key] = row.record_image_id
    used = {}
    todo_ids = [p.capture_id for p in todo]
    for start in range(0, len(todo_ids), 500):
        for image_id, cid in (db.query(RecordImage.id, RecordImage.capture_id)
                              .filter(RecordImage.capture_id.in_(todo_ids[start:start + 500]))):
            used[cid] = image_id
    db.commit()
    taken = ([{"ordinal": p.ordinal, "volumeOrder": img.volume_order, "key": keys[img.filename],
               "reason": "key_already_held", "held_by_image_id": held[keys[img.filename]]}
              for p, img in todo_images if keys[img.filename] in held]
             + [{"ordinal": p.ordinal, "capture_id": p.capture_id, "reason": "capture_id_in_use",
                 "image_id": used[p.capture_id]} for p in todo if p.capture_id in used])
    if taken:
        raise RebuildRefused("a planned key or capture id is already recorded on this unit", taken)

    by_id = {p.capture_id: p for p in planned}
    stray = []
    for cid, entry in in_manifest.items():
        p = by_id.get(cid)
        files = entry.get("files") or []
        if p is None:
            # A capture taken after an earlier run raised the counter (or one
            # whose commit failed) carries a number above counterAfter.
            numbers = [_capture_number(stem, Path(str(f.get("relative_path", ""))).name)
                       for f in files if isinstance(f, dict)]
            if (counter_before < counter_after or not numbers
                    or not all(n is not None and n > counter_after for n in numbers)):
                stray.append({"capture_id": cid, "reason": "manifest_capture_not_planned"})
        elif cid not in done and _line_differences(p, entry):
            # A line a failed commit left: the append-only manifest keeps it, so it must be the plan's.
            stray.append({"capture_id": cid, "ordinal": p.ordinal, "reason": "manifest_line_differs",
                          "fields": _line_differences(p, entry)})
    if stray:
        raise RebuildRefused(f"the manifest of volume {stem!r} holds captures this bundle does not plan", stray)

    sizes, problems = _listing_problems(planned, keys)
    if problems:
        raise RebuildRefused(f"the bucket does not hold {len(problems)} planned object(s) as planned", problems)

    def files(p: Planned) -> List[dict]:
        return [{"volume_order": img.volume_order, "filename": img.filename, "role": img.role,
                 "key": keys[img.filename], "bytes": img.size} for img in p.images]

    report = {"filename_stem": stem, "collection_id": collection_id, "bundle": str(bundle_dir),
              "applied": apply, "records": len(planned), "images": len(_all_images(planned)),
              "created": [], "skipped": [],
              "counter_before": counter_before, "counter_after": max(counter_before, counter_after),
              "freed_bytes": sum(sizes[keys[img.filename]] for _, img in todo_images), "manifest": None,
              "resequenced": [], "thumbnails_recopied": [
                  d["thumbnail_path"] for p in planned if p.capture_id in done
                  for d in done[p.capture_id]["images"].values() if d["thumbnail_missing"]]}
    for p in planned:
        if p.capture_id in done:
            report["skipped"].append({"ordinal": p.ordinal, "mode": p.mode, "capture_id": p.capture_id,
                                      "record_id": done[p.capture_id]["record_id"]})
    if not apply:
        report["created"] = [{"ordinal": p.ordinal, "mode": p.mode, "capture_id": p.capture_id,
                              "pair_id": p.pair_id, "files": files(p)} for p in todo]
        return report

    from app.core import audit, capture_counter
    from app.core.offsite_free import _RunManifest
    from capture.project_manager import ensure_volume_dirs

    # Before the first record, so that a capture into the volume during the
    # run takes counterAfter + 1 and never a planned number. A capture that
    # landed between the checks and this raise took a planned number: the
    # counter it left is not the one read above, and the run stops before
    # any record is written. Only the raise itself is then committed, and
    # the counter never decreases.
    if counter_after > counter_before:
        raised = capture_counter.raise_capture_counter(db, stem, counter_after, apply=True)
        if raised["counter_before"] != counter_before:
            raise RebuildRefused(
                f"the capture counter of volume {stem!r} changed from {counter_before} to "
                f"{raised['counter_before']} while the run was being checked: a capture was taken into the "
                f"volume, under a number the bundle plans. No record was written; the counter is now "
                f"{counter_after}. Resolve that capture before running the command again.",
                [{"reason": "counter_changed", "read": counter_before, "found": raised["counter_before"]}])
    on_card = [{"ordinal": p.ordinal, "volumeOrder": img.volume_order, "path": paths[img.filename],
                "reason": "master_on_card"} for p, img in todo_images if Path(paths[img.filename]).exists()]
    if on_card:
        raise RebuildRefused("a planned master is already on the card", on_card)
    ensure_volume_dirs(stem, root)
    report["thumbnails_recopied"] = []
    for p, img in _all_images(planned):
        d = done[p.capture_id]["images"][img.filename] if p.capture_id in done else None
        if d is not None and d["thumbnail_missing"]:
            _recopy_thumbnail(img.thumbnail, Path(d["thumbnail_path"]))
            report["thumbnails_recopied"].append(d["thumbnail_path"])
    freed_manifest = _RunManifest(stem, "freed", directory=volume_dir / METADATA_DIR)
    already_freed = _freed_keys(volume_dir)
    for p, img in _all_images(planned):
        if p.capture_id in done and keys[img.filename] not in already_freed:
            d = done[p.capture_id]["images"][img.filename]
            freed_manifest.write(_freed_entry(d["image_id"], done[p.capture_id]["record_id"], d["file_path"],
                                              keys[img.filename], img.sha1, d["freed_bytes"], d["freed_at"]))

    for p in todo:
        created = _write_record(db, p, collection_id=collection_id, stem=stem, project_name=project_name,
                                keys=keys, paths=paths, sizes=sizes, volume_dir=volume_dir,
                                in_manifest=in_manifest)
        for img, image_id in zip(p.images, created["image_ids"]):
            freed_manifest.write(_freed_entry(image_id, created["record_id"], paths[img.filename],
                                              keys[img.filename], img.sha1, sizes[keys[img.filename]],
                                              created["freed_at"]))
        report["created"].append({"ordinal": p.ordinal, "mode": p.mode, "capture_id": p.capture_id,
                                  "pair_id": p.pair_id, "files": files(p), "record_id": created["record_id"],
                                  "image_ids": created["image_ids"]})
    report["manifest"] = str(freed_manifest.path) if freed_manifest.path else None
    report["resequenced"] = _resequence_others(db, collection_id, planned)
    db.expire_all()
    report["counter_after"] = db.get(Collection, collection_id).capture_counter
    db.commit()
    audit.log_event(db, level="INFO", category=CATEGORY, action=REBUILT_ACTION, actor=None, subject=stem,
                    detail=(f"collection_id={collection_id} records={len(planned)} images={report['images']} "
                            f"created={len(report['created'])} skipped={len(report['skipped'])} "
                            f"freed_bytes={report['freed_bytes']} resequenced={len(report['resequenced'])} "
                            f"counter={counter_before}->{report['counter_after']}"))
    return report


def _write_record(db: Session, p: Planned, *, collection_id: int, stem: str, project_name: str,
                  keys: Dict[str, str], paths: Dict[str, str], sizes: Dict[str, int], volume_dir: Path,
                  in_manifest: Dict[str, dict]) -> dict:
    """Write one record whole: its thumbnails, its manifest line, then its rows in one commit (module header)."""
    from app.core import image_upload
    from app.core.volume_stem import locked_volume_query
    from app.models.offsite_object import OffsiteObject
    from app.models.record import Record, RecordImage
    from capture.manifestHandler import append_manifest_lines

    thumbs: List[Path] = []
    committed = False
    try:
        for img in p.images:
            thumbs.append(_copy_thumbnail(img.thumbnail, volume_dir / THUMBNAILS_DIR))
        if p.capture_id not in in_manifest:
            line = _capture_line(p, project_name)
            append_manifest_lines(volume_dir, [line])
            in_manifest[p.capture_id] = json.loads(line)

        volume = locked_volume_query(db, collection_id).first()
        if volume is None or volume.filename_stem != stem:
            db.rollback()
            raise RebuildFailed(f"volume {stem!r} changed while it was rebuilt; nothing more was written")
        at = datetime.now(timezone.utc)
        record = Record(
            # capture's own defaults for a capture without a title (app.api.cameras
            # trigger_capture and trigger_dual_capture)
            title=(f"{project_name} - Dual capture" if p.mode == "dual"
                   else f"{project_name} - {Path(p.images[0].filename).stem}"),
            collection_id=collection_id,
            sequence=p.ordinal,
            capture_mode=p.mode,
            created_at=p.captured_at,
        )
        db.add(record)
        db.flush()
        images = []
        for img, thumb in zip(p.images, thumbs):
            image = RecordImage(
                record_id=record.id,
                filename=img.filename,
                file_path=paths[img.filename],
                thumbnail_path=str(thumb),
                file_size=img.size,
                format=Path(img.filename).suffix.lower().lstrip("."),
                resolution_width=img.width,
                resolution_height=img.height,
                capture_id=p.capture_id,
                pair_id=p.pair_id,
                role=img.role,
                rotate_deg=img.rotate_deg,
                created_at=p.captured_at,
                master_freed_at=at,
                master_freed_bytes=sizes[keys[img.filename]],
            )
            db.add(image)
            db.flush()
            images.append(image)
        # Every image's offsite row but the last's is added here, and the
        # last's through _claim, which locks the record's row, reads that
        # image's state again under it and commits the whole record. The
        # state it compares names the record's volume, which every image of
        # the record shares.
        for img, image in zip(p.images[:-1], images[:-1]):
            db.add(OffsiteObject(record_image_id=image.id, kind="capture", object_key=keys[img.filename],
                                 local_sha1=img.sha1, verified_at=at))
        last, last_image = p.images[-1], images[-1]
        state = image_upload._image_state(db, last_image.id)
        try:
            image_upload._claim(db, None, image_upload.Work(last_image.id, "capture", stem,
                                                            paths[last.filename]),
                                keys[last.filename], state, local_sha1=last.sha1, verified_at=at)
            committed = True
        except image_upload._ImageMoved as exc:
            raise RebuildFailed(f"the record {p.ordinal} ({', '.join(i.filename for i in p.images)}) changed "
                                "while it was written; it was rolled back") from exc
        result = {"record_id": record.id, "image_ids": [i.id for i in images], "freed_at": at}
    except BaseException:
        db.rollback()
        if not committed:  # once committed, the rows name the thumbnails
            for thumb in thumbs:
                thumb.unlink(missing_ok=True)
        raise
    return result


# ---------------------------------------------------------------------------
# Operator command
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.core.rebuild_volume",
        description="Rebuild an empty volume from paired JPGs whose masters are already in the bucket "
                    "(uploads and deletes nothing).",
    )
    parser.add_argument("stem", help="the volume's file name stem")
    parser.add_argument("bundle", help="the bundle directory (plan.json, images.jsonl, thumbnails)")
    parser.add_argument("--apply", action="store_true",
                        help="write the records; without it, only report what would be written")
    args = parser.parse_args(argv)

    from app.core.db import SessionLocal, init_db

    # As in app.core.image_upload.main: nothing has imported app.main, which
    # is what registers the models for the backend.
    init_db()
    db = SessionLocal()
    try:
        report = rebuild_volume(db, args.stem, args.bundle, apply=args.apply)
    except IntegrityError as exc:
        db.rollback()
        print(f"error: the database refused a record's rows, which were rolled back: {exc.orig}",
              file=sys.stderr)
        return 2
    except (RebuildRefused, RebuildFailed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        problems = getattr(exc, "problems", None)
        if problems:
            print(json.dumps(problems, indent=2, default=str), file=sys.stderr)
        return 2
    finally:
        db.close()
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
