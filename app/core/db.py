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
	"""
	Import all models to register them with SQLAlchemy.
	
	Database tables should be created and managed through Alembic migrations,
	not through create_all(). This function only ensures models are imported.
	
	Raises:
		ImportError: If any model cannot be imported (app should not start)
	"""
	# Import model modules so they register with Base
	import app.models.document  # noqa: F401
	import app.models.camera  # noqa: F401
	import app.models.project  # noqa: F401
	import app.models.user  # noqa: F401
	
	# Note: Tables are created via Alembic migrations, not create_all()
	# Run: alembic upgrade head

