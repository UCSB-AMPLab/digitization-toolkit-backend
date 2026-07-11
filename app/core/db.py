from typing import Generator

from sqlalchemy import create_engine
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
# and 500s the user before the pool discards it — unacceptable when no one is around
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

