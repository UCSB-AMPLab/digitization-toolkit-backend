from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import QueuePool

from app.core.config import settings


# Build DATABASE_URL from individual components
DATABASE_URL = (
	f"postgresql+psycopg://{settings.DATABASE_USER}:{settings.DATABASE_PASSWORD}"
	f"@{settings.DATABASE_HOST}:{settings.DATABASE_PORT}/{settings.DATABASE_NAME}"
)


# Create engine
# This is an unattended, offline-first appliance (Raspberry Pi): Postgres runs in a
# Docker container that can restart under the backend (manual `docker restart`, or the
# container restart policy), which leaves pooled connections stale. Without
# pool_pre_ping, the next request to draw a stale connection raises OperationalError
# and 500s the user before the pool discards it - unacceptable when no one is around
# to retry. pool_recycle proactively retires connections before they go stale.
engine = create_engine(
	DATABASE_URL,
	echo=False,
	poolclass=QueuePool,
	pool_pre_ping=True,
	pool_recycle=1800,
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# An export is a statement about a collection at an instant, so while one is
# being assembled the collection does not change (NEH-260). Sixteen routes can
# change something a bag asserts, and the previous attempt at a rule spread over
# many routes was defeated through the ones nobody had edited -- so the rule is
# here, where every write passes, rather than in each of them.
#
# Registered against SessionLocal itself, which the test suite rebinds to its
# SQLite engine rather than replacing (tests/conftest.py), so the guard is in
# force where it is tested. The import is inside the body because the guard
# imports the models and the models import this module.
@event.listens_for(SessionLocal, "before_flush")
def _refuse_writes_to_an_exporting_collection(session, flush_context, instances):
	from app.core.export_guard import assert_no_export_holds

	assert_no_export_holds(session)

# Base for models
Base = declarative_base()


def get_db() -> Generator:
	db = SessionLocal()
	try:
		yield db
	finally:
		db.close()


def init_db() -> None:
	"""
	Import all models to register them with SQLAlchemy.
	
	Database tables should be created and managed through Alembic migrations,
	not through create_all(). This function only ensures models are imported.
	
	Raises:
		ImportError: If any model cannot be imported (app should not start)
	"""
	# Import model modules so they register with Base
	import app.models.record  # noqa: F401
	import app.models.camera  # noqa: F401
	import app.models.project  # noqa: F401
	import app.models.collection  # noqa: F401
	import app.models.user           # noqa: F401
	import app.models.system_log     # noqa: F401
	import app.models.project_member # noqa: F401

	# Note: Tables are created via Alembic migrations, not create_all()
	# Run: alembic upgrade head

