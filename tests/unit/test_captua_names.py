"""Names the backend writes that carry the software's name.

    python -m pytest tests/unit/test_captua_names.py
"""

from types import SimpleNamespace

import pytest

import capture.service as capture_service
from app.core.export_service import source_organization


@pytest.mark.unit
def test_a_bag_without_a_project_names_captua_as_its_source_organization():
    assert source_organization(None) == "Captua"


@pytest.mark.unit
def test_a_bag_with_a_project_names_the_project():
    assert source_organization(SimpleNamespace(name="Archivo de Rionegro")) == "Archivo de Rionegro"


@pytest.mark.unit
def test_the_preview_flush_removes_files_under_both_names(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_service, "_PREVIEW_TMP_DIR", tmp_path)
    for name in ("captua_preview_c0.jpg", "dtk_preview_c1.jpg", "other.jpg"):
        (tmp_path / name).write_bytes(b"x")

    assert capture_service.flush_preview_tmp() == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.jpg"]


@pytest.mark.unit
def test_the_preview_file_is_written_under_the_captua_name():
    assert capture_service._PREVIEW_PREFIX == "captua_preview_c"
