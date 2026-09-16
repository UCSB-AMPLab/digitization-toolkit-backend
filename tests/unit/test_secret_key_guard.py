"""Guard against booting outside dev with a default/empty SECRET_KEY (NEH-54)."""

from types import SimpleNamespace

import pytest

from app.core.config import _guard_secret_key


def _cfg(app_env: str, secret_key: str) -> SimpleNamespace:
    """Minimal stand-in for Settings with just the fields the guard reads."""
    return SimpleNamespace(APP_ENV=app_env, SECRET_KEY=secret_key)


@pytest.mark.parametrize("app_env", ["dev", "development", "test", "testing", "local", "DEV"])
def test_dev_environments_allow_default_key(app_env):
    # Dev must keep booting on the shipped default so nothing needs configuring locally.
    _guard_secret_key(_cfg(app_env, "dev-secret-change-me"))


@pytest.mark.parametrize("bad_key", ["", "   ", "dev-secret-change-me", "secret-key-here-change-in-production"])
def test_production_rejects_default_or_empty_key(bad_key):
    with pytest.raises(RuntimeError):
        _guard_secret_key(_cfg("production", bad_key))


def test_unknown_environment_is_treated_as_production():
    with pytest.raises(RuntimeError):
        _guard_secret_key(_cfg("staging", "dev-secret-change-me"))


def test_production_with_strong_key_boots():
    _guard_secret_key(_cfg("production", "a3f9c1e7b2d84605f1c9a7e3b5d02f18"))
