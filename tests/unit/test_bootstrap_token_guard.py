"""Gate first-user admin bootstrap behind a local bootstrap token."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.auth import _authorize_bootstrap


def _cfg(app_env: str, bootstrap_token: str) -> SimpleNamespace:
    """Minimal stand-in for Settings with just the fields the gate reads."""
    return SimpleNamespace(APP_ENV=app_env, BOOTSTRAP_TOKEN=bootstrap_token)


def test_dev_without_configured_token_allows_bootstrap():
    # Dev keeps the tokenless first-user flow so local setup needs no configuration
    _authorize_bootstrap(None, config=_cfg("dev", ""))


def test_production_without_configured_token_fails_closed():
    with pytest.raises(HTTPException) as exc:
        _authorize_bootstrap("anything", config=_cfg("production", ""))
    assert exc.value.status_code == 503


def test_production_with_matching_token_allows_bootstrap():
    _authorize_bootstrap("s3cret-token", config=_cfg("production", "s3cret-token"))


@pytest.mark.parametrize("provided", [None, "", "wrong-token"])
def test_production_rejects_missing_or_wrong_token(provided):
    with pytest.raises(HTTPException) as exc:
        _authorize_bootstrap(provided, config=_cfg("production", "s3cret-token"))
    assert exc.value.status_code == 401


def test_configured_token_is_enforced_even_in_dev():
    # If an operator sets a token, dev enforces it too (opt-in)
    with pytest.raises(HTTPException):
        _authorize_bootstrap("wrong", config=_cfg("dev", "s3cret-token"))
