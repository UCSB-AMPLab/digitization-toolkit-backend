"""Every operator command registers every model before it opens a session.

`python -m app.core.image_upload <command>` and `python -m
app.core.volume_layout` run in an interpreter that has not imported
app.main, and app/models/__init__.py imports nothing. Without registering
the models first, the first query fails while SQLAlchemy configures the
mappers: relationship("Project") on Record cannot find Project.
tests/conftest.py imports every model, which hides that failure in-process, so
each command runs here in a fresh interpreter (cli_session_worker.py) against
the SQLite file the db_session fixture has created.

Version: 0.1.0-beta
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("cli_session_worker.py")
STEM = "co-test-cli"


@pytest.fixture
def seeded(db_session):
    from app.models.collection import Collection
    from app.models.project import Project

    project = Project(name="CLI")
    db_session.add(project)
    db_session.commit()
    db_session.add(Collection(name="V", project_id=project.id, filename_stem=STEM))
    db_session.commit()
    return db_session.get_bind().url.database


def _run(module, dbfile, *args):
    return subprocess.run(
        [sys.executable, str(WORKER), module, dbfile, *args],
        cwd=BACKEND, env=dict(os.environ), capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("module,args", [
    ("app.core.image_upload", ("raise-capture-counter", STEM, "5")),
    ("app.core.image_upload", ("mark-freed", STEM)),
    ("app.core.image_upload", ("backfill-preview-digests", STEM)),
    ("app.core.volume_layout", ("--dry-run",)),
])
def test_command_runs_in_a_fresh_interpreter(seeded, module, args):
    result = _run(module, seeded, *args)
    assert "failed to locate a name" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert isinstance(json.loads(result.stdout), dict)


def test_dry_run_raises_nothing(seeded, db_session):
    from app.models.collection import Collection

    result = _run("app.core.image_upload", seeded, "raise-capture-counter", STEM, "5")
    assert result.returncode == 0, result.stderr
    db_session.expire_all()
    assert db_session.query(Collection).filter_by(filename_stem=STEM).one().capture_counter == 0


def test_init_db_imports_every_model_module():
    """A model added to app/models/ without a line in init_db() would be missing from the commands."""
    modules = sorted(f"app.models.{p.stem}" for p in (BACKEND / "app" / "models").glob("*.py")
                     if p.stem not in ("__init__", "base"))
    code = ("import sys; from app.core.db import init_db; init_db(); "
            f"print([m for m in {modules!r} if m not in sys.modules])")
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=dict(os.environ),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
