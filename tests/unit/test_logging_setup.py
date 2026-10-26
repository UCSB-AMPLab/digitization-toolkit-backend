"""The backend's own INFO lines reach stderr, and so the systemd journal.

uvicorn's logging config covers only the `uvicorn`, `uvicorn.error` and
`uvicorn.access` loggers. Without app.core.logging_setup, a logger under `app.`
or `capture.` has no handler and the root logger's WARNING level, so its INFO
lines (the startup `Volume layout:` report among them) are dropped, and its
warnings come out through Python's last-resort handler with no level or name.
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
    assert "CAPTUA_LOG_LEVEL" in err and "CHATTY" in err
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


# Placeholder JWT-shaped strings; no real token appears in these tests.
FAKE_TOKEN = "abc.def.ghi"


@pytest.fixture
def uvicorn_access_capture():
    """A StringIO handler on uvicorn.access with uvicorn's own AccessFormatter,
    so the assertions read the line as the journal receives it."""
    import io

    from uvicorn.logging import AccessFormatter

    saved = {n: list(logging.getLogger(n).filters) for n in logging_setup.UVICORN_LOGGERS}
    access = logging.getLogger("uvicorn.access")
    saved_level = access.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    yield stream
    access.removeHandler(handler)
    access.setLevel(saved_level)
    for name, filters in saved.items():
        logging.getLogger(name).filters[:] = filters


def _access(target: str) -> None:
    # The same call shape as uvicorn's h11 and httptools protocols.
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "172.30.0.4:53480", "GET", target, "1.1", 200
    )


def test_access_line_masks_the_token_and_keeps_other_params(restore_loggers, uvicorn_access_capture):
    configure_logging("INFO")

    _access(f"/records/images/5/thumbnail?token={FAKE_TOKEN}&size=s")

    line = uvicorn_access_capture.getvalue()
    assert FAKE_TOKEN not in line
    assert '"GET /records/images/5/thumbnail?token=***&size=s HTTP/1.1" 200 OK' in line


def test_access_line_without_a_token_is_unchanged(restore_loggers, uvicorn_access_capture):
    configure_logging("INFO")

    _access("/records/images/5/thumbnail?size=s&page=2")
    _access("/cameras")

    lines = uvicorn_access_capture.getvalue().splitlines()
    assert lines == [
        '172.30.0.4:53480 - "GET /records/images/5/thumbnail?size=s&page=2 HTTP/1.1" 200 OK',
        '172.30.0.4:53480 - "GET /cameras HTTP/1.1" 200 OK',
    ]


@pytest.mark.parametrize(
    "target, expected",
    [
        (f"/x?access_token={FAKE_TOKEN}", "/x?access_token=***"),
        (f"/x?size=s&TOKEN={FAKE_TOKEN}", "/x?size=s&TOKEN=***"),
        (f"/x?%74oken={FAKE_TOKEN}&a=1", "/x?%74oken=***&a=1"),
        (f"/x?token={FAKE_TOKEN}&token={FAKE_TOKEN}", "/x?token=***&token=***"),
        (f"/x?mytoken={FAKE_TOKEN}", f"/x?mytoken={FAKE_TOKEN}"),
        ("/x?token=", "/x?token="),
        ("/token=abc", "/token=abc"),
    ],
)
def test_redact_query_secrets(target, expected):
    assert logging_setup.redact_query_secrets(target) == expected


def test_preformatted_message_is_masked_too(restore_loggers, caplog):
    configure_logging("INFO")

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logging.getLogger("uvicorn.error").info(f'1.2.3.4:5 - "WebSocket /ws?token={FAKE_TOKEN}" 403')

    assert FAKE_TOKEN not in caplog.text
    assert '"WebSocket /ws?token=***" 403' in caplog.text


def test_filter_is_added_once(restore_loggers, uvicorn_access_capture):
    configure_logging("INFO")
    configure_logging("INFO")

    for name in logging_setup.UVICORN_LOGGERS:
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, logging_setup.RedactQuerySecretsFilter)]
        assert len(filters) == 1, name
