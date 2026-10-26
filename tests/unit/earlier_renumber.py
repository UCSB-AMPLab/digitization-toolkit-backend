"""The state a file-renaming renumber leaves on a unit, built by hand for tests.

Units in service may hold volumes whose files a renumber renamed to
`<prefix>_<NNNN><suffix>` (the prefix from the volume's signatura or name),
with each RAW master's `_preview.jpg` sidecar beside it under the new name,
the rows pointing at the new names, and a relocation record in the capture
manifest for each renamed master with a capture id. Renumbering rewrites
records' `sequence` only and writes none of this, but the migration and the
integrity index still read it. This helper produces that state.

Version: 0.1.0-beta
"""

import os
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from app.core.paths import preview_sidecar_of, resolve_within_storage


def rename_as_an_earlier_renumber(db, collection, *, write_records: bool = True,
                                  prefix: Optional[str] = None) -> List[Tuple[int, Path, Path]]:
    """Rename the volume's image files as a file-renaming renumber does, and commit.

    Order: records by `sequence` (NULLs last), then creation time; within a
    record, left/single/overview before right, then image id. Each image gets
    the next number, zero-padded to at least four digits. With
    `write_records=False` no relocation record is written, as when such a
    renumber could not append one. Returns (image id, old path, new path).
    """
    from capture.project_manager import secure_project_filename
    from app.core.storage_ops import (
        FixityIndexOnce, _raw_sidecar_to_carry, relocation_of, write_relocation_records,
    )
    from app.models.record import Record

    records = (db.query(Record).filter(Record.collection_id == collection.id)
               .order_by(Record.sequence.nullslast(), Record.created_at).all())
    role_priority = {"left": 0, "single": 0, "overview": 0, "right": 1}
    images = [img for rec in records
              for img in sorted(rec.images, key=lambda i: (role_priority.get(i.role, 0), i.id))]
    if prefix is None:
        signatura = ((collection.archival_metadata or {}).get("signatura") or "").strip()
        prefix = secure_project_filename(signatura or collection.name)
    width = max(4, len(str(len(images))))

    plan = []
    for n, img in enumerate(images, start=1):
        src = resolve_within_storage(img.file_path)
        plan.append((img, src, src.parent / f"{prefix}_{str(n).zfill(width)}{src.suffix}"))
    fixity_index = FixityIndexOnce()
    relocations = [r for r in (relocation_of(img, src, dst, fixity_index) for img, src, dst in plan) if r]

    moves = [(src, dst) for _, src, dst in plan]
    for _, src, dst in plan:
        sidecar = _raw_sidecar_to_carry(src)
        if sidecar is not None:
            moves.append((sidecar, preview_sidecar_of(dst)))
    temps = [(src, src.parent / f".renumber_{uuid.uuid4().hex}{src.suffix}", dst) for src, dst in moves]
    for src, temp, _ in temps:
        os.replace(src, temp)
    for _, temp, dst in temps:
        os.replace(temp, dst)

    for img, _, dst in plan:
        img.filename = dst.name
        img.file_path = str(dst)
        img.thumbnail_path = None
    db.commit()
    if write_records:
        write_relocation_records(relocations)
    return [(img.id, src, dst) for img, src, dst in plan]
