import logging
from datetime import datetime

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def log_event(
    db: Session,
    *,
    level: str,
    category: str,
    action: str,
    actor: str | None = None,
    subject: str | None = None,
    detail: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """
    Record an audit event in system_logs. Always commits immediately.

    Safe to call from any route - exceptions are suppressed so that a
    logging failure never breaks the main request.

    Parameters
    ----------
    db       : active SQLAlchemy session
    level    : "INFO" | "WARN" | "ERR"
    category : "access" | "activity" | "capture" | "system"
    action   : machine-readable verb, e.g. "login_success", "project_created"
    actor    : username (or None for system-triggered events)
    subject  : affected entity name (project name, username, ...)
    detail   : free-form extra context (IP address, record count, ...)
    created_at : when the event happened, if that is not "now". Left out, the
               column takes its server default, now(), which in PostgreSQL is
               the start of the transaction - fine for a route that records an
               event it just performed, wrong for a long job whose session
               opened before the work began.
    """
    try:
        from app.models.system_log import SystemLog  # late import avoids circular refs

        entry = SystemLog(
            level=level,
            category=category,
            action=action,
            actor=actor[:150]   if actor   else None,
            subject=subject[:300] if subject else None,
            detail=detail[:500]  if detail  else None,
        )
        if created_at is not None:
            entry.created_at = created_at
        db.add(entry)
        db.commit()
    except Exception:
        logger.exception("Failed to write audit log entry")
        try:
            db.rollback()
        except Exception:
            pass
