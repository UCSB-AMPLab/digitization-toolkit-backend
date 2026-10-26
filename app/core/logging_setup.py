"""Send the backend's own log lines to stderr, where systemd's journal reads them.

uvicorn configures only its own loggers (`uvicorn`, `uvicorn.error`,
`uvicorn.access`). Without a handler of their own, the loggers under `app.`
and `capture.` take the root logger's WARNING level, so INFO lines such as the
startup `Volume layout:` report do not reach the journal, and warnings come
out through Python's last-resort handler with no level or logger name.

The handler goes on the `app` and `capture` package loggers rather than on
root. On root it would also carry third-party libraries' INFO lines (picamera2's
logger is set to DEBUG in capture.service, and its records propagate), and it
would print a second copy of anything else that later configures root. The
package loggers keep propagating, so pytest's caplog and any root handler still
see their records.

The `capture_service` and `project_manager` loggers are outside both packages
and keep writing only to their rotating files under CAPTUA_LOG_DIR.

uvicorn's own loggers get one thing from here: a filter that masks session
tokens in the request targets they print. `<img src>` cannot send an
Authorization header, so image, thumbnail and download URLs carry the JWT as
`?token=` (NEH-61), and uvicorn's access line prints the target in full to the
journal, which anyone with a shell on the unit can read. The filter sits on the
loggers rather than on uvicorn's handlers so it holds whatever handlers
`--log-config` or uvicorn's defaults install.
"""

import logging
import re
import sys
from urllib.parse import unquote_plus

from app.core.config import settings

APP_LOGGERS = ("app", "capture")
HANDLER_NAME = "captua-stderr"
FORMAT = "%(levelname)s %(name)s: %(message)s"

# uvicorn.error carries the WebSocket handshake lines, which print the target too.
UVICORN_LOGGERS = ("uvicorn.access", "uvicorn.error")
# Matched after percent-decoding, because the query parser decodes `%74oken`
# to `token` before FastAPI looks the parameter up; case-insensitively, so a
# client's `TOKEN=` is masked too.
SECRET_QUERY_PARAMS = frozenset({"token", "access_token"})
REDACTED = "***"


# A query parameter: preceded by `?` or `&`, its value ending at the next
# parameter, fragment, whitespace or quote, so it also works on a whole
# request line such as `"GET /x?token=... HTTP/1.1"`.
_QUERY_PARAM = re.compile(r"(?<=[?&])([^=&#\s\"']+)=([^&#\s\"']+)")


def _mask(match: re.Match) -> str:
    key = match.group(1)
    if unquote_plus(key).lower() in SECRET_QUERY_PARAMS:
        return f"{key}={REDACTED}"
    return match.group(0)


def redact_query_secrets(text: str) -> str:
    """Return `text` with the value of every secret query parameter masked.

    The path and every other parameter are kept as they were, so the line still
    says which image and which size was asked for.
    """
    if "=" not in text:
        return text
    return _QUERY_PARAM.sub(_mask, text)


class RedactQuerySecretsFilter(logging.Filter):
    """Masks secret query parameters in a record's message and string arguments.

    uvicorn passes the request target as a separate argument
    (`client, method, target, http_version, status`), and its AccessFormatter
    reads the arguments rather than the formatted message, so the arguments
    are what has to change.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            redacted = tuple(redact_query_secrets(a) if isinstance(a, str) else a for a in args)
            if redacted != args:
                record.args = redacted
        if isinstance(record.msg, str):
            record.msg = redact_query_secrets(record.msg)
        return True


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is at the moment of each record, so a
    replaced stderr (pytest's capture, a reloader) is followed, not kept."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stderr


def _resolve_level(name: str) -> tuple[int, bool]:
    level = logging.getLevelNamesMapping().get(name.strip().upper())
    if level is None:
        return logging.INFO, False
    return level, True


def configure_logging(level: str | None = None) -> logging.Handler:
    """Attach one stderr handler to each application package logger, and the
    token filter to uvicorn's loggers.

    Safe to call more than once: a second call finds the handler by name and
    only applies the level.
    """
    requested = settings.LOG_LEVEL if level is None else level
    numeric, known = _resolve_level(requested)

    handler: logging.Handler | None = None
    for name in APP_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(numeric)
        existing = next((h for h in logger.handlers if h.get_name() == HANDLER_NAME), None)
        if existing is not None:
            handler = existing
            continue
        if handler is None:
            handler = _StderrHandler()
            handler.set_name(HANDLER_NAME)
            handler.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(handler)

    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactQuerySecretsFilter) for f in logger.filters):
            logger.addFilter(RedactQuerySecretsFilter())

    if not known:
        logging.getLogger("app.core.logging_setup").warning(
            "CAPTUA_LOG_LEVEL=%r is not a logging level; using INFO", requested
        )
    return handler
