"""One projects root per capture, and uploads already in uploads/.

The occupied-directory and rescan checks are in
test_stem_directory_occupied and test_volume_manifest_repair.
"""

import hashlib
import io
import json
from pathlib import Path

import pytest

from tests.unit.test_volume_layout_by_stem import (  # noqa: F401
    STEMS, _FakeBackend, _as_admin, _capture_records, capture_settings, data_dir, legacy,
)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _new_layout_capture(db, root, collection_id, stem, name, cid):
    from app.models.record import Record, RecordImage

    vol = root / stem
    for d in ("captures", "previews", "thumbnails", "metadata"):
        (vol / d).mkdir(parents=True, exist_ok=True)
    f = vol / "captures" / name
    f.write_bytes(_jpeg())
    with (vol / "metadata" / "manifest.jsonl").open("a") as m:
        m.write(json.dumps({"capture_id": cid, "files": [{"relative_path": f"captures/{name}", "sha256": _sha(f)}]}) + "\n")
    rec = Record(title="n", collection_id=collection_id, status="captured", capture_mode="single")
    db.add(rec)
    db.commit()
    img = RecordImage(record_id=rec.id, filename=name, file_path=str(f), format="jpg", role="single", capture_id=cid)
    db.add(img)
    db.commit()
    return img


@pytest.mark.unit
def test_a_storage_switch_during_a_capture_keeps_the_record_beside_the_image(
        db_session, legacy, monkeypatch, tmp_path, mock_camera_config):
    """The root is read once for the image and again for the manifest (capture/service.py)."""
    import capture.service as service
    from app.core import storage_override

    monkeypatch.setattr(service, "get_backend", lambda: _FakeBackend(raw=False))
    other = tmp_path / "drive" / "dtk-projects"
    other.mkdir(parents=True)
    state = {"root": None}
    monkeypatch.setattr(storage_override, "get_storage_override_or_fallback", lambda: (state["root"], False))
    monkeypatch.setattr(storage_override, "override_root_problem", lambda p: None)
    real = service.capture_image

    def capture_then_switch(*a, **k):
        out = real(*a, **k)
        state["root"] = str(other)  # an admin activates the drive while the capture finishes
        return out

    monkeypatch.setattr(service, "capture_image", capture_then_switch)
    path, capture_id, _ = service.single_capture_image(
        "Proj", mock_camera_config, check_camera=False, collection_name="Fondo A", stem=STEMS["a2"],
        filename_index=f"{STEMS['a2']}-img_0001")
    image_root = Path(str(path)).resolve().parents[2]
    manifests = [m for m in (legacy["root"], other) if (m / STEMS["a2"] / "metadata" / "manifest.jsonl").exists()
                 and capture_id in (m / STEMS["a2"] / "metadata" / "manifest.jsonl").read_text()]
    print("\nSWITCH image under", image_root, "record under", manifests)
    assert manifests == [image_root]


@pytest.mark.unit
def test_an_image_already_under_uploads_in_a_stemmed_volume_is_served_scanned_gated_and_deleted(
        client, db_session, legacy, data_dir, monkeypatch):
    from app.api import records
    from app.core.export_service import _fixity_gate
    from app.core.integrity import run_integrity_check
    from app.core.volume_layout import migrate_volume_layout
    from app.models.record import Record, RecordImage

    monkeypatch.setattr(records.settings, "DATA_DIR", str(data_dir))
    up = data_dir / "uploads"
    up.mkdir()
    f = up / "0123abcd.jpg"
    f.write_bytes(_jpeg())
    rec = Record(title="u", collection_id=legacy["a1"].id, status="captured", capture_mode="single")
    db_session.add(rec)
    db_session.commit()
    img = RecordImage(record_id=rec.id, filename="scan.jpg", file_path=str(f), format="jpg", role="single")
    db_session.add(img)
    db_session.commit()

    report = migrate_volume_layout(db_session)
    c = _as_admin(client)
    codes = {p: c.get(f"/records/images/{img.id}/{p}").status_code for p in ("file", "preview", "thumbnail")}
    _fixity_gate([db_session.get(RecordImage, img.id)])
    result = run_integrity_check(db_session, verify_hashes=True)
    d = c.delete(f"/records/images/{img.id}")
    print("\nUPLOADS", codes, "skipped", [s["reason"] for s in report["skipped"] if s["image_id"] == img.id],
          "missing", result["missing_files"], "orphans", [Path(p).name for p in result["orphan_files"]],
          "delete", d.status_code, "file left", f.exists())
    assert all(v == 200 for v in codes.values())
    assert result["missing_files"] == [] and [Path(p).name for p in result["orphan_files"]] == ["stray.jpg"]
    assert d.status_code in (200, 204) and not f.exists()
