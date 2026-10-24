"""Run by test_list_import.py in a subprocess: one import of a fixed list id, with or without flock.

Usage: python list_import_race_worker.py <import|import_noflock> <sqlite file> <sha256>
DTK_DATA_DIR comes from the environment. Prints one RESULT line.
"""
import os
import sys
import time

sys.path.insert(0, os.getcwd())

import app.core.list_import as li  # noqa: E402

LIST_ID = "00000000-0000-4000-8000-0000000000aa"

mode, dbfile, sha = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "import_noflock":
    def _nolock(fd, op):
        raise OSError(37, "No locks available")
    li.fcntl.flock = _nolock
    _real_write = li._write_hash_file

    def _slow_write(directory, digest):
        # Both processes pass the first-hash check before either writes.
        time.sleep(2)
        return _real_write(directory, digest)
    li._write_hash_file = _slow_write

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import user, project, project_member, collection, record, camera, system_log, offsite_object  # noqa: E402,F401

engine = create_engine(f"sqlite:///{dbfile}")
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
if mode == "import_noflock":
    # Without flock an import may not create a project (the project lock
    # cannot be taken, ProjectLockUnavailable), so the project exists
    # already; this race is about the first hash file.
    from app.models.project import Project
    db.add(Project(name="Carrera"))
    db.commit()
doc = {"contract": "fisqua-captua-exchange", "version": "1.1", "kind": "digitisation-list",
       "id": LIST_ID, "list": {"name": "Carrera"},
       "items": [{"referenceCode": "xx-test-001", "filenameStem": "xx-test-001", "title": "T"}]}
try:
    r = li.import_list(db, doc, "a", True, file_sha256=sha)
    print("RESULT", sha[:6], "created", r["created"], flush=True)
except li.ListIdReused:
    print("RESULT", sha[:6], "ListIdReused", flush=True)
