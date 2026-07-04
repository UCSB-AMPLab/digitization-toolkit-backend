"""
Tests that captured images and their sha256 provenance manifest land in the
same sanitized directory tree, even when the project/collection name has
capitals, spaces, or accents.
"""

import json
import pytest


class _StubConfig:
    """Minimal stand-in for CameraConfig used by generate_manifest_record."""
    encoding = "jpg"
    camera_index = 0

    def to_dict(self):
        return {"camera_index": self.camera_index, "encoding": self.encoding}


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """Point the capture layout helpers at a temporary projects root."""
    from capture import project_manager as pm
    monkeypatch.setattr(pm, "PROJECTS_ROOT", tmp_path)
    return tmp_path


# ==============================================================================
# Path layout helpers
# ==============================================================================

class TestLayoutHelpers:
    def test_project_root_is_sanitized(self, projects_root):
        from capture.project_manager import project_capture_root
        root = project_capture_root("Río Negro Ñandú")
        assert root == projects_root / "rio_negro_nandu"

    def test_image_dir_sanitizes_project_and_collection(self, projects_root):
        from capture.project_manager import image_output_dir
        img_dir = image_output_dir("Río Negro Ñandú", "Caja 1")
        assert img_dir == projects_root / "rio_negro_nandu" / "caja_1" / "images" / "main"

    def test_image_dir_without_collection(self, projects_root):
        from capture.project_manager import image_output_dir
        img_dir = image_output_dir("Rionegro")
        assert img_dir == projects_root / "rionegro" / "images" / "main"


# ==============================================================================
# Manifest / image directory agreement
# ==============================================================================

def _write_fake_image(img_dir, name):
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / name
    img_path.write_bytes(b"fake-jpeg-bytes")
    return img_path


class TestManifestSharesImageDirectory:
    @pytest.mark.parametrize("project_name,collection_name", [
        ("Rionegro", None),                 # capital, no collection
        ("Río Negro Ñandú", "Caja 1"),      # accents + spaces, with collection
    ])
    def test_manifest_paths_resolve_to_images(self, projects_root, project_name, collection_name):
        from capture.project_manager import project_capture_root, image_output_dir
        from capture.manifestHandler import generate_manifest_record, append_manifest_record

        img_dir = image_output_dir(project_name, collection_name)
        img_path = _write_fake_image(img_dir, "20260101_000000_000_c0.jpg")

        project_root = project_capture_root(project_name)
        record = generate_manifest_record(
            project_name=project_name,
            img_paths=[img_path],
            cam_configs=[_StubConfig()],
            times=[0.1],
            project_root=project_root,
        )
        append_manifest_record(project_root, record)

        # Manifest lands under the same sanitized project root as the images
        manifest_path = project_root / "metadata" / "manifest.jsonl"
        assert manifest_path.exists()

        # Every file path in the manifest, resolved from project_root, is the real image
        assert record.files, "expected at least one file in the manifest record"
        for f in record.files:
            resolved = (project_root / f.relative_path).resolve()
            assert resolved == img_path.resolve()
            assert resolved.exists()

    def test_manifest_on_disk_matches_record(self, projects_root):
        from capture.project_manager import project_capture_root, image_output_dir
        from capture.manifestHandler import generate_manifest_record, append_manifest_record

        project_name = "Río Negro Ñandú"
        collection_name = "Caja 1"
        img_dir = image_output_dir(project_name, collection_name)
        img_path = _write_fake_image(img_dir, "20260101_000000_000_c0.jpg")

        project_root = project_capture_root(project_name)
        record = generate_manifest_record(
            project_name=project_name,
            img_paths=[img_path],
            cam_configs=[_StubConfig()],
            times=[0.1],
            project_root=project_root,
        )
        append_manifest_record(project_root, record)

        manifest_path = project_root / "metadata" / "manifest.jsonl"
        entry = json.loads(manifest_path.read_text(encoding="utf-8").strip())
        rel = entry["files"][0]["relative_path"]
        # Relative path includes the collection subdir and resolves to the image
        assert (project_root / rel).resolve() == img_path.resolve()
