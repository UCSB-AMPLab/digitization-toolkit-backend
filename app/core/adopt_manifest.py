"""The capture-manifest lines of an adoption (NEH-387).

An adoption (app.core.adopt_captures) renames every file of the source
volume with the target's stem and moves it under the target's root. The
capture-time digests stay in the source's `metadata/manifest.jsonl`, under
the old paths, and that manifest is never rewritten. adopt_manifest_lines
builds the lines that give the moved files their digests under the new
paths: lines to append to the target's manifest, and lines to append to the
source's. It reads nothing and writes nothing; the apply step appends both
lists with capture.manifestHandler.append_manifest_lines.

The target's lines are copies of the source's lines of the capture ids the
plan's images have:

* Every capture record (capture.manifestHandler.is_capture_record, which
  includes an `upload` record) of those capture ids, every field kept, with
  each `files[].relative_path` rewritten to the file's new path relative to
  the target's root (its master, preview or `.raw` buffer, compared
  case-insensitively; the number is kept), `pair_id` rewritten where the
  plan gives the image a new pair id, and `copied_from`: the source's
  manifest relative to the projects root, `<source stem>/metadata/manifest.jsonl`,
  or, for a line that is itself a copy, the original's, as
  app.core.volume_layout._records_to_copy marks its copies. The fixity index
  reads a copy as it reads any capture record, so each moved file has its
  digest by its new path and name; the startup repair of a volume's manifest
  counts it as the capture's record; the export bag takes it only for a
  capture whose capture-time line no manifest holds
  (app.core.integrity.capture_record_lines). A capture record that names a
  path no rename maps raises ValueError: the plan refuses such a source
  (`source_manifest_path_unmapped`).
* Every `preview_digest` record of those capture ids whose paths all map,
  rewritten and marked the same way, so a preview whose digest was
  backfilled after capture has it at its new path too. One whose path maps
  to no rename names a file the adoption does not move, and is left out.

Relocation records of those capture ids are not copied: they name the
source's earlier names, and the copied capture records already give every
file its digest by its new path. Lines of capture ids no image of the plan
has are not copied.

The source's lines are one relocation record per master moved
(capture.manifestHandler.append_relocation_record's fields): its capture id,
its image id, the sha256 the source's capture records give the master at
its old path, `from` its old path and `to` its new one, both relative to the
source's root (`../<target stem>/captures/<new name>`), as
app.core.storage_ops.relocation_of writes them for a move between volumes,
and `relocated_at`, the time the caller passes. The fixity index takes a
relocation record that names its image as that image's digest, follows it
to the master's preview and to a JPEG master's `.raw` buffer, and every
other reader of a manifest leaves relocation records out, so the source's
manifest records where its captures went and gives the index the same
digests the target's copies give. A master for which the source's capture
records hold no sha256 gets no relocation record, as relocation_of writes
none.

Colour card shots (app.core.adopt_captures.ShotRename) are carried the
same way. The target gets a copy of every `calibration` line
(capture.manifestHandler.CALIBRATION_RECORD_TYPE) of a planned shot's
capture id whose paths all map to the shot's renames, each
`files[].relative_path` rewritten to the new path relative to the target's
root and marked `copied_from`; a line naming a path no rename maps is left
out, since a shot's digest is its row's and its line is the record of how
it was taken. The source gets one relocation record per shot moved, in the
form of a page's with `shot_id` in place of `image_id`: its capture id, the
shot's sha256 as its row gives it, `from` and `to` relative to the source's
root, and `relocated_at`. The fixity index takes a relocation record only
for an image it names, so a shot's adds no digest anywhere. The shots'
lines come after the pages', so a source with no shot gives exactly the
lines it gave before.

Version: 0.3.3
"""
import copy
import os
from pathlib import Path


def _key(path, root: Path) -> str:
    """`path` relative to `root`, normalised and casefolded, as a manifest's paths are matched with the renames."""
    return os.path.normpath(os.path.relpath(path, root)).casefold()


def _line_key(relative: str) -> str:
    return os.path.normpath(relative).casefold()


def adopt_manifest_lines(plan, source_lines: list, *, adopted_at: str) -> tuple:
    """(lines for the target's manifest, lines for the source's), as dicts, for a plan that may adopt (module header).

    `source_lines` are the decoded lines of the source's
    `metadata/manifest.jsonl`; anything in it that is not a dict is skipped.
    `adopted_at` is the `relocated_at` of the source's relocation records.
    Neither argument is changed. Raises ValueError for a plan with a
    refusal, and for a capture record of the plan's capture ids that names
    a path no rename maps.
    """
    if not plan.may_adopt:
        raise ValueError(f"the adoption is refused: {', '.join(plan.refusals)}")
    if not plan.renames:
        return [], []
    target, source = _page_lines(plan, source_lines, adopted_at)
    shot_target, shot_source = _shot_lines(plan, source_lines, adopted_at)
    return target + shot_target, source + shot_source


def _shot_lines(plan, source_lines: list, adopted_at: str) -> tuple:
    """(target lines, source lines) for the plan's colour card shots (module header)."""
    from capture.manifestHandler import CALIBRATION_RECORD_TYPE, RELOCATION_RECORD_TYPE

    shots = getattr(plan, "shot_renames", None) or []
    if not shots:
        return [], []
    src_root = plan.renames[0].old_path.parent.parent
    tgt_root = plan.renames[0].new_path.parent.parent
    new_paths = {}
    for r in shots:
        for old, new in ((r.old_path, r.new_path), (r.old_preview_path, r.new_preview_path),
                         (r.old_raw_buffer_path, r.new_raw_buffer_path)):
            if old is not None and new is not None:
                new_paths[_key(old, src_root)] = os.path.relpath(new, tgt_root)
    owned = {r.capture_id for r in shots}
    provenance = f"{plan.source_stem}/metadata/manifest.jsonl"
    target = []
    for line in source_lines:
        if not (isinstance(line, dict) and line.get("record_type") == CALIBRATION_RECORD_TYPE
                and line.get("capture_id") in owned):
            continue
        entry = copy.deepcopy(line)
        files = [f for f in entry.get("files") or [] if isinstance(f, dict) and isinstance(f.get("relative_path"), str)]
        if any(_line_key(f["relative_path"]) not in new_paths for f in files):
            continue
        for f in files:
            f["relative_path"] = new_paths[_line_key(f["relative_path"])]
        entry.setdefault("copied_from", provenance)
        target.append(entry)
    source = [{
        "record_type": RELOCATION_RECORD_TYPE,
        "capture_id": r.capture_id,
        "shot_id": r.shot_id,
        "sha256": r.sha256,
        "from": os.path.relpath(r.old_path, src_root),
        "to": os.path.relpath(r.new_path, src_root),
        "relocated_at": adopted_at,
    } for r in shots if r.sha256]
    return target, source


def _page_lines(plan, source_lines: list, adopted_at: str) -> tuple:
    """(target lines, source lines) for the plan's images (module header)."""
    from capture.manifestHandler import PREVIEW_DIGEST_RECORD_TYPE, RELOCATION_RECORD_TYPE, is_capture_record

    src_root = plan.renames[0].old_path.parent.parent
    tgt_root = plan.renames[0].new_path.parent.parent
    new_paths = {}
    for r in plan.renames:
        for old, new in ((r.old_path, r.new_path), (r.old_preview_path, r.new_preview_path),
                         (r.old_raw_buffer_path, r.new_raw_buffer_path)):
            if old is not None and new is not None:
                new_paths[_key(old, src_root)] = os.path.relpath(new, tgt_root)
    new_pair_ids = {r.old_pair_id: r.new_pair_id for r in plan.renames
                    if r.old_pair_id and r.new_pair_id and r.new_pair_id != r.old_pair_id}
    owned = {r.capture_id for r in plan.renames if r.capture_id}
    provenance = f"{plan.source_stem}/metadata/manifest.jsonl"

    target = []
    digests = {}   # (capture id, matched path) -> the sha256 a capture record gives it
    for line in source_lines:
        if not isinstance(line, dict) or line.get("capture_id") not in owned:
            continue
        kind = line.get("record_type")
        capture = is_capture_record(line)
        if kind == RELOCATION_RECORD_TYPE or not (capture or kind == PREVIEW_DIGEST_RECORD_TYPE):
            continue
        entry = copy.deepcopy(line)
        unmapped = None
        for f in entry.get("files") or []:
            if not (isinstance(f, dict) and isinstance(f.get("relative_path"), str)):
                continue
            matched = _line_key(f["relative_path"])
            if capture and f.get("sha256"):
                digests[(line["capture_id"], matched)] = f["sha256"]
            if matched not in new_paths:
                unmapped = f["relative_path"]
                continue
            f["relative_path"] = new_paths[matched]
        if unmapped is not None:
            if capture:
                raise ValueError(f"the capture record of {line['capture_id']} names {unmapped}, "
                                 "which no rename of the plan maps")
            continue
        if entry.get("pair_id") in new_pair_ids:
            entry["pair_id"] = new_pair_ids[entry["pair_id"]]
        entry.setdefault("copied_from", provenance)
        target.append(entry)

    source = []
    for r in plan.renames:
        sha = digests.get((r.capture_id, _key(r.old_path, src_root))) if r.capture_id else None
        if sha is None:
            continue
        source.append({
            "record_type": RELOCATION_RECORD_TYPE,
            "capture_id": r.capture_id,
            "image_id": r.image_id,
            "sha256": sha,
            "from": os.path.relpath(r.old_path, src_root),
            "to": os.path.relpath(r.new_path, src_root),
            "relocated_at": adopted_at,
        })
    return target, source
