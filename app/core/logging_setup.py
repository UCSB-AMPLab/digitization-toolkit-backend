"""Send the backend's own log lines to stderr, where systemd's journal reads them.

uvicorn configures only its own loggers (`uvicorn`, `uvicorn.error`,
`uvicorn.access`). Everything under `app.` and `capture.` was left with no
handler and the root logger's WARNING level, so INFO lines such as the startup
`Volume layout:` report never reached the journal, and warnings came out
through Python's last-resort handler with no level or logger name.

The handler goes on the `app` and `capture` package loggers rather than on
root. On root it would also carry third-party libraries' INFO lines (picamera2's
logger is set to DEBUG in capture.service, and its records propagate), and it
would print a second copy of anything else that later configures root. The
package loggers keep propagating, so pytest's caplog and any root handler still
see their records.

The `capture_service` and `project_manager` loggers are outside both packages
and keep writing only to their rotating files under DTK_LOG_DIR.
"""

import logging
import sys

from app.core.config import settings

APP_LOGGERS = ("app", "capture")
HANDLER_NAME = "dtk-stderr"
FORMAT = "%(levelname)s %(name)s: %(message)s"


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
    """Attach one stderr handler to each application package logger.

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

    if not known:
        logging.getLogger("app.core.logging_setup").warning(
            "DTK_LOG_LEVEL=%r is not a logging level; using INFO", requested
        )
    return handler
