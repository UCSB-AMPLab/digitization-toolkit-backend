#!/usr/bin/env python3
"""
Regression test for NEH-125: the capture write path must resolve the active
projects root at call time so a runtime storage-drive switch (POST
/system/storage/activate, which flips the storage override consulted by
Settings.projects_dir) takes effect immediately for new captures.

Before the fix, capture/project_manager.py captured
``PROJECTS_ROOT = settings.projects_dir`` as a module-level constant at import
time, so after an operator activated an external drive new captures kept
writing to the OLD root while the registry/delete/export/storage-panel readers
followed the NEW root — splitting a project across two disks.

Run with: python -m pytest tests/unit/test_projects_root_live.py
"""

from pathlib import Path

import capture.project_manager as pm


def test_project_capture_root_follows_runtime_override(tmp_path, monkeypatch):
    """Activating a drive at runtime must move new captures to the new root
    without re-importing the capture modules."""
    new_root = tmp_path / "external-drive" / "dtk-projects"
    new_root.mkdir(parents=True, exist_ok=True)

    # Simulate what POST /system/storage/activate does: set the storage
    # override that Settings.projects_dir consults on every access. We patch
    # get_storage_override where projects_dir imports it (inside the property).
    monkeypatch.setattr(
        "app.core.storage_override.get_storage_override",
        lambda: str(new_root),
    )

    # No re-import of pm — the module has been imported once already, exactly
    # as it is in the long-running backend process when the drive is switched.
    resolved = pm.project_capture_root("My Project")

    assert resolved == new_root / "my_project"
    # And the manifest/image helpers built on top of it follow the same root.
    assert pm.image_output_dir("My Project").is_relative_to(new_root)


def test_projects_root_reflects_override_change_between_calls(tmp_path, monkeypatch):
    """Two successive resolutions with different overrides must land on
    different disks — proving the root is read per call, not cached."""
    root_a = tmp_path / "disk-a"
    root_b = tmp_path / "disk-b"
    root_a.mkdir()
    root_b.mkdir()

    monkeypatch.setattr(
        "app.core.storage_override.get_storage_override", lambda: str(root_a)
    )
    first = pm.project_capture_root("proj")

    monkeypatch.setattr(
        "app.core.storage_override.get_storage_override", lambda: str(root_b)
    )
    second = pm.project_capture_root("proj")

    assert first == root_a / "proj"
    assert second == root_b / "proj"
    assert first != second
