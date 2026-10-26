"""The settings' two environment names (app.core.config).

Every prefixed setting is read as CAPTUA_<X> and as DTK_<X>, the name field
units carry in their .env and offsite.env. A
CAPTUA_ name wins over its DTK_ name wherever either is set; within one family
the process environment wins over the files, and offsite.env over ../.env.

    python -m pytest tests/unit/test_env_names.py

Version: 0.1.0-beta
"""

import logging

import pytest
from pydantic import AliasChoices

from app.core import config
from app.core.config import NEW_PREFIX, OLD_PREFIX, Settings, aliased_suffixes, log_environment_names


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """No CAPTUA_ or DTK_ setting from the shell or CI leaks into these tests."""
    for suffix in aliased_suffixes():
        monkeypatch.delenv(f"{NEW_PREFIX}{suffix}", raising=False)
        monkeypatch.delenv(f"{OLD_PREFIX}{suffix}", raising=False)


def _env_file(path, **values):
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    return path


@pytest.mark.unit
def test_every_old_name_has_a_new_name_read_first():
    """No field accepts DTK_<X> without CAPTUA_<X>, and CAPTUA_<X> comes first."""
    for name, field in Settings.model_fields.items():
        assert not name.upper().startswith(OLD_PREFIX), f"field {name} still carries the old prefix"
        alias = field.validation_alias
        if alias is None:
            continue
        choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
        old = [c for c in choices if isinstance(c, str) and c.upper().startswith(OLD_PREFIX)]
        for old_name in old:
            new_name = NEW_PREFIX + old_name[len(OLD_PREFIX):]
            assert new_name in choices, f"{name} accepts {old_name} but not {new_name}"
            assert choices.index(new_name) < choices.index(old_name), f"{name} reads {old_name} first"


@pytest.mark.unit
def test_the_old_name_alone_is_still_read(monkeypatch):
    monkeypatch.setenv("DTK_OFFSITE_TENANT", "rionegro")
    monkeypatch.setenv("DTK_DATA_DIR", "/srv/old")
    s = Settings(_env_file=None)
    assert s.OFFSITE_TENANT == "rionegro"
    assert s.DATA_DIR == "/srv/old"


@pytest.mark.unit
def test_the_new_name_alone_is_read(monkeypatch):
    monkeypatch.setenv("CAPTUA_OFFSITE_TENANT", "rionegro")
    monkeypatch.setenv("CAPTUA_LOG_DIR", "/srv/logs")
    s = Settings(_env_file=None)
    assert s.OFFSITE_TENANT == "rionegro"
    assert s.LOG_DIR == "/srv/logs"


@pytest.mark.unit
def test_the_new_name_wins_in_the_same_place(monkeypatch):
    monkeypatch.setenv("DTK_OFFSITE_TENANT", "old")
    monkeypatch.setenv("CAPTUA_OFFSITE_TENANT", "new")
    assert Settings(_env_file=None).OFFSITE_TENANT == "new"


@pytest.mark.unit
def test_the_new_name_in_a_file_wins_over_the_old_name_in_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DTK_OFFSITE_TENANT", "old")
    env = _env_file(tmp_path / ".env", CAPTUA_OFFSITE_TENANT="new")
    assert Settings(_env_file=(env,)).OFFSITE_TENANT == "new"


@pytest.mark.unit
def test_the_new_name_in_dot_env_wins_over_the_old_name_in_offsite_env(tmp_path):
    dot_env = _env_file(tmp_path / ".env", CAPTUA_OFFSITE_TENANT="new")
    offsite = _env_file(tmp_path / "offsite.env", DTK_OFFSITE_TENANT="old")
    assert Settings(_env_file=(dot_env, offsite)).OFFSITE_TENANT == "new"


@pytest.mark.unit
def test_within_one_family_the_environment_still_wins_over_the_files(monkeypatch, tmp_path):
    monkeypatch.setenv("DTK_OFFSITE_BWLIMIT", "1M")
    env = _env_file(tmp_path / ".env", DTK_OFFSITE_BWLIMIT="500k")
    assert Settings(_env_file=(env,)).OFFSITE_BWLIMIT == "1M"


@pytest.mark.unit
def test_within_one_family_offsite_env_still_wins_over_dot_env(tmp_path):
    dot_env = _env_file(tmp_path / ".env", DTK_OFFSITE_TENANT="first", DATABASE_PASSWORD="kept")
    offsite = _env_file(tmp_path / "offsite.env", DTK_OFFSITE_TENANT="second")
    s = Settings(_env_file=(dot_env, offsite))
    assert s.OFFSITE_TENANT == "second"
    assert s.DATABASE_PASSWORD == "kept"


@pytest.mark.unit
def test_the_env_files_are_still_dot_env_and_offsite_env():
    assert Settings.model_config["env_file"] == ("../.env", "/var/lib/dtk/offsite.env")
    assert config.ENV_FILES == ("../.env", "/var/lib/dtk/offsite.env")


@pytest.mark.unit
def test_a_unit_env_file_with_only_dtk_names_is_read_unchanged(tmp_path):
    """offsite.env as provision-rclone.sh has written it on units, every key DTK_."""
    offsite = _env_file(
        tmp_path / "offsite.env",
        DTK_OFFSITE_REMOTE="b2unit:bucket",
        DTK_OFFSITE_TENANT="rionegro",
        DTK_RCLONE_CONFIG="/var/lib/dtk/rclone.conf",
        DTK_FISQUA_ENV="staging",
        DTK_FISQUA_TOKEN="t0ken",
    )
    s = Settings(_env_file=(tmp_path / "missing.env", offsite))
    assert s.OFFSITE_REMOTE == "b2unit:bucket"
    assert s.OFFSITE_TENANT == "rionegro"
    assert s.RCLONE_CONFIG == "/var/lib/dtk/rclone.conf"
    assert s.FISQUA_ENV == "staging"
    assert s.FISQUA_TOKEN.get_secret_value() == "t0ken"


@pytest.mark.unit
def test_a_conflict_is_warned_once_by_name_without_the_values(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("CAPTUA_FISQUA_TOKEN", "new-secret-token")
    offsite = _env_file(tmp_path / "offsite.env", DTK_FISQUA_TOKEN="old-secret-token")
    with caplog.at_level(logging.INFO, logger="app.core.config"):
        report = log_environment_names((offsite,))
    assert report["conflicts"] == ["FISQUA_TOKEN"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "CAPTUA_FISQUA_TOKEN" in warnings[0] and "DTK_FISQUA_TOKEN" in warnings[0]
    assert "secret-token" not in caplog.text
    assert Settings(_env_file=(offsite,)).FISQUA_TOKEN.get_secret_value() == "new-secret-token"


@pytest.mark.unit
def test_the_same_value_under_both_names_is_not_a_conflict(monkeypatch, caplog):
    monkeypatch.setenv("CAPTUA_OFFSITE_TENANT", "rionegro")
    monkeypatch.setenv("DTK_OFFSITE_TENANT", "rionegro")
    with caplog.at_level(logging.INFO, logger="app.core.config"):
        report = log_environment_names(())
    assert report["conflicts"] == []
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.unit
@pytest.mark.parametrize(
    "variables, expected",
    [
        ({"DTK_OFFSITE_TENANT": "x"}, "under the older DTK_ names (DTK_OFFSITE_TENANT)"),
        ({"CAPTUA_OFFSITE_TENANT": "x"}, "under CAPTUA_ names (CAPTUA_OFFSITE_TENANT)"),
        ({"CAPTUA_OFFSITE_TENANT": "x", "DTK_RCLONE_BIN": "y"},
         "under CAPTUA_ names (CAPTUA_OFFSITE_TENANT) and under the older DTK_ names (DTK_RCLONE_BIN)"),
        ({}, "No CAPTUA_ or DTK_ settings are set"),
    ],
)
def test_the_family_read_is_logged_once_without_values(monkeypatch, caplog, variables, expected):
    for key, value in variables.items():
        monkeypatch.setenv(key, value)
    with caplog.at_level(logging.INFO, logger="app.core.config"):
        log_environment_names(())
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert expected in infos[0]
    for value in variables.values():
        assert f"={value}" not in infos[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    "variables, skipped",
    [
        ({"CAPTUA_SKIP_SCHEMA_CHECK": "1"}, True),
        ({"DTK_SKIP_SCHEMA_CHECK": "1"}, True),
        ({"CAPTUA_SKIP_SCHEMA_CHECK": "0", "DTK_SKIP_SCHEMA_CHECK": "1"}, False),
        ({}, False),
    ],
)
def test_the_schema_check_skip_reads_both_names(monkeypatch, variables, skipped):
    from app.core.schema import _skip_requested

    monkeypatch.delenv("CAPTUA_SKIP_SCHEMA_CHECK", raising=False)
    monkeypatch.delenv("DTK_SKIP_SCHEMA_CHECK", raising=False)
    for key, value in variables.items():
        monkeypatch.setenv(key, value)
    assert _skip_requested() is skipped
