from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import QueuePool

from app.core.config import settings


# Build DATABASE_URL from settings (settings.DATABASE_URL may already be set)
DATABASE_URL = getattr(settings, "DATABASE_URL", None)
if not DATABASE_URL:
	DATABASE_URL = (
		f"postgresql://{settings.DATABASE_USER}:{settings.DATABASE_PASSWORD}@{settings.DATABASE_HOST}:{settings.DATABASE_PORT}/{settings.DATABASE_NAME}"
	)


# Create engine
engine = create_engine(
	DATABASE_URL,
	echo=False,
	poolclass=QueuePool,
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
	"""Import models to ensure they are registered on the Base metadata, then create tables."""
	# Import model modules so they register with Base
	try:
		import app.models.document  # noqa: F401
		import app.models.camera  # noqa: F401
		import app.models.project  # noqa: F401
		import app.models.user  # noqa: F401
	except Exception:
		raise # We cannot skip this; if models don't import, tables won't be created

	Base.metadata.create_all(bind=engine)
