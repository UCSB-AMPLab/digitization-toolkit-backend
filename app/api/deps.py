# Version: 0.1.0-beta

from typing import Generator

from app.core.db import get_db


def get_db_dependency() -> Generator:
	"""Wrapper around app.core.db.get_db for FastAPI dependencies."""
	yield from get_db()
