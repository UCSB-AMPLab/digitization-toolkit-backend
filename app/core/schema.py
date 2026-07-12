"""Startup guard that refuses to serve against an out-of-date database schema.

Running new code against a database that was never migrated to the current
Alembic head silently uses a stale schema until some endpoint 500s in the field,
with no operator around to diagnose it. This asserts at startup that the database
is stamped at head, turning a late, obscure 500 into a clear, immediate failure.

Set DTK_SKIP_SCHEMA_CHECK=1 to bypass (e.g. tooling that manages the schema out
of band). If Alembic or the config cannot be loaded the check is skipped with a
warning; only a genuine revision mismatch aborts startup.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_ROOT / "alembic.ini"


class SchemaOutOfDateError(RuntimeError):
    """Raised when the database schema revision does not match the Alembic head."""


def _skip_requested() -> bool:
    val = os.environ.get("DTK_SKIP_SCHEMA_CHECK", "")
    return val.strip().lower() not in ("", "0", "false", "no")


def assert_schema_at_head() -> None:
    """Abort startup unless the database is migrated to the Alembic head revision."""
    if _skip_requested():
        logger.warning("[INFO] Schema head check skipped (DTK_SKIP_SCHEMA_CHECK set)")
        return

    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from alembic.runtime.migration import MigrationContext
        from app.core.db import engine
    except Exception as e:
        # Without Alembic or the engine we cannot verify; do not block startup on that
        logger.warning(f"[WARNING] Schema head check unavailable, skipping: {e}")
        return

    if not _ALEMBIC_INI.exists():
        logger.warning(f"[WARNING] alembic.ini not found at {_ALEMBIC_INI}, skipping schema check")
        return

    script = ScriptDirectory.from_config(Config(str(_ALEMBIC_INI)))
    heads = set(script.get_heads())

    try:
        with engine.connect() as conn:
            current = set(MigrationContext.configure(conn).get_current_heads())
    except Exception as e:
        # An unreachable database is a distinct failure; surface it rather than mask it.
        raise SchemaOutOfDateError(f"Could not read the database schema revision: {e}") from e

    if current == heads:
        logger.info(f"[OK] Database schema at head ({', '.join(sorted(heads)) or 'none'})")
        return

    raise SchemaOutOfDateError(
        f"Database schema is out of date: database at {sorted(current) or 'no revision'}, "
        f"code expects head {sorted(heads)}. Run 'alembic upgrade head' (or update.sh) before starting the backend."
    )
