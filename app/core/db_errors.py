"""Map SQLAlchemy IntegrityError to sanitized API responses.

The appliance is LAN-exposed on port 80, so constraint-violation responses must
never echo the offending SQL statement or its bound parameters. This module
turns an IntegrityError into a 409 carrying only the violated constraint name
and a human-readable message.
"""
from typing import Optional
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

# Friendly messages keyed by the canonical (Postgres) constraint name. Anything
# not listed falls back to _DEFAULT_MESSAGE; the constraint name is still returned.
_CONSTRAINT_MESSAGES = {
	"check_record_single_parent": "A record belongs to either a project or a collection, not both.",
	"camera_settings_record_image_id_key": "Camera settings already exist for this record.",
}

# SQLite names unnamed constraints differently from Postgres; map the substrings
# it prints back to the canonical name so responses are uniform across engines.
_SQLITE_ALIASES = {
	"check_record_single_parent": "check_record_single_parent",
	"camera_settings.record_image_id": "camera_settings_record_image_id_key",
}

_DEFAULT_MESSAGE = "The request conflicts with a database constraint."


def constraint_name(exc: IntegrityError) -> Optional[str]:
	"""Best-effort canonical name of the violated constraint, or None. Never returns SQL or bound parameters."""
	orig = getattr(exc, "orig", None)
	if orig is None:
		return None
	# Postgres (psycopg): structured diagnostics carry the constraint name directly.
	name = getattr(getattr(orig, "diag", None), "constraint_name", None)
	if name:
		return name
	# SQLite: recover the canonical name from the short message text.
	text = str(orig)
	for token, canonical in _SQLITE_ALIASES.items():
		if token in text:
			return canonical
	return None


def integrity_conflict(exc: IntegrityError) -> HTTPException:
	"""Build a sanitized 409 for a DB integrity violation, exposing the constraint name and a friendly message only."""
	name = constraint_name(exc)
	detail = {"message": _CONSTRAINT_MESSAGES.get(name, _DEFAULT_MESSAGE)}
	if name:
		detail["constraint"] = name
	return HTTPException(status_code=409, detail=detail)
