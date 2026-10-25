"""Run by test_cli_entry_points.py in a subprocess: one operator command against a SQLite file.

Usage: python cli_session_worker.py <module> <sqlite file> [args ...]

A fresh interpreter, so nothing has imported the models the way
tests/conftest.py does. The worker only points app.core.db.SessionLocal at
the SQLite file (the settings build a Postgres URL) and then calls the
module's main() with the remaining arguments, as `python -m <module>` would.
It imports no model itself.
"""
import importlib
import os
import sys

sys.path.insert(0, os.getcwd())

from sqlalchemy import create_engine  # noqa: E402

import app.core.db as db  # noqa: E402

module_name, dbfile, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
db.SessionLocal.configure(bind=create_engine(f"sqlite:///{dbfile}"))
sys.exit(importlib.import_module(module_name).main(argv))
