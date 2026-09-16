"""Guard against booting outside dev with a placeholder DATABASE_PASSWORD"""

from types import SimpleNamespace

import pytest

from app.core.config import _guard_database_password


def _cfg(app_env: str, db_password: str) -> SimpleNamespace:
    """Minimal stand-in for Settings with just the fields the guard reads."""
    return SimpleNamespace(APP_ENV=app_env, DATABASE_PASSWORD=db_password)


@pytest.mark.parametrize("app_env", ["dev", "development", "test", "testing", "local", "DEV"])
def test_dev_environments_allow_placeholder(app_env):
    # Dev must keep booting on the shipped placeholder so nothing needs configuring locally
    _guard_database_password(_cfg(app_env, "password"))


@pytest.mark.parametrize("bad_pw", ["", "   ", "password", "change-this-in-production"])
def test_production_rejects_placeholder_or_empty(bad_pw):
    with pytest.raises(RuntimeError):
        _guard_database_password(_cfg("production", bad_pw))


def test_unknown_environment_is_treated_as_production():
    with pytest.raises(RuntimeError):
        _guard_database_password(_cfg("staging", "password"))


def test_production_with_strong_password_boots():
    _guard_database_password(_cfg("production", "b2d84605f1c9a7e3b5d02f18a3f9c1e7"))
