"""The backend's own INFO lines reach stderr, and so the systemd journal.

uvicorn's logging config covers only the `uvicorn`, `uvicorn.error` and
`uvicorn.access` loggers. Before app.core.logging_setup, a logger under `app.`
or `capture.` had no handler and the root logger's WARNING level, so its INFO
lines (the startup `Volume layout:` report among them) were dropped, and its
warnings came out through Python's last-resort handler with no level or name.
"""

import logging

import pytest

from app.core import logging_setup
from app.core.logging_setup import HANDLER_NAME, configure_logging


def _ours(logger_name: str) -> list[logging.Handler]:
    return [h for h in logging.getLogger(logger_name).handlers if h.get_name() == HANDLER_NAME]


@pytest.fixture
def restore_loggers():
    saved = {
        name: (logging.getLogger(name).level, list(logging.getLogger(name).handlers))
        for name in logging_setup.APP_LOGGERS
    }
    yield
    for name, (level, handlers) in saved.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers[:] = handlers


def test_app_startup_configures_app_loggers():
    import app.main  # noqa: F401  (the import is the startup configuration)

    for name in logging_setup.APP_LOGGERS:
        assert len(_ours(name)) == 1, name


def test_info_from_app_logger_reaches_stderr_with_level_and_name(restore_loggers, capsys):
    configure_logging("INFO")

    logging.getLogger("app.core.volume_layout").info("Volume layout: probe")
    logging.getLogger("capture.camera").info("camera probe")
    logging.getLogger("app.core.volume_layout").debug("debug probe")

    err = capsys.readouterr().err
    assert "INFO app.core.volume_layout: Volume layout: probe" in err
    assert "INFO capture.camera: camera probe" in err
    assert "debug probe" not in err


def test_configuring_twice_does_not_duplicate_handlers(restore_loggers, capsys):
    configure_logging("INFO")
    configure_logging("INFO")

    for name in logging_setup.APP_LOGGERS:
        assert len(_ours(name)) == 1, name

    logging.getLogger("app.api.records").info("once only")
    assert capsys.readouterr().err.count("once only") == 1


def test_level_setting_is_applied(restore_loggers, capsys):
    configure_logging("WARNING")

    logging.getLogger("app.core.schema").info("quiet")
    logging.getLogger("app.core.schema").warning("loud")

    err = capsys.readouterr().err
    assert "quiet" not in err
    assert "WARNING app.core.schema: loud" in err


def test_unknown_level_falls_back_to_info_and_says_so(restore_loggers, capsys):
    configure_logging("CHATTY")

    logging.getLogger("app.core.schema").info("still here")

    err = capsys.readouterr().err
    assert "DTK_LOG_LEVEL" in err and "CHATTY" in err
    assert "INFO app.core.schema: still here" in err


def test_uvicorn_and_root_loggers_are_left_alone(restore_loggers):
    root_handlers = list(logging.getLogger().handlers)
    root_level = logging.getLogger().level
    uvicorn_handlers = {n: list(logging.getLogger(n).handlers) for n in ("uvicorn", "uvicorn.access")}

    configure_logging("INFO")

    assert logging.getLogger().handlers == root_handlers
    assert logging.getLogger().level == root_level
    for n, handlers in uvicorn_handlers.items():
        assert logging.getLogger(n).handlers == handlers
