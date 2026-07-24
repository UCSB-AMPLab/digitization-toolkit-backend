"""Regression test for NEH-128: a traversal-laden project_name must not let the
manifest write escape projects_dir.

The manifest root is derived through secure_project_filename (fixed in NEH-45);
these tests guard against a regression that would reintroduce a raw path join.
"""
import pytest

from capture import project_manager as pm
from capture.project_manager import project_capture_root
from capture.manifestHandler import ProjectInfo, append_manifest_record

TRAVERSAL_NAMES = [
    "../../../../home/pi/.config/foo",
    "../../secret",
    "..",
    "a/../../b",
    "/etc/passwd",
    "....//....//etc",
]


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    """Redirect the projects root at its source so nothing touches real data."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(pm, "_projects_root", lambda: root)
    return root.resolve()


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_capture_root_stays_inside_projects_dir(projects_root, name):
    resolved = project_capture_root(name).resolve()
    assert resolved.is_relative_to(projects_root), f"{name!r} escaped: {resolved}"


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_manifest_write_stays_inside_projects_dir(projects_root, name):
    append_manifest_record(
        project_capture_root(name), ProjectInfo(project_name=name), record_type="project"
    )
    manifests = list(projects_root.rglob("project_manifest.jsonl"))
    assert manifests, "manifest was not written inside projects_dir"
    for f in manifests:
        assert f.resolve().is_relative_to(projects_root)
