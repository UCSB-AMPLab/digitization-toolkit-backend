# Version: 0.2.1

import logging
import os
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Every setting the backend reads under a prefix has two names: CAPTUA_<X>,
# and DTK_<X>, the name the env files on field units carry. Both are read, and
# a CAPTUA_ name wins over its DTK_ name wherever either is set (the process
# environment, ../.env or offsite.env). The units' files keep their DTK_ names
# because a rollback restores code that reads only those.
NEW_PREFIX = "CAPTUA_"
OLD_PREFIX = "DTK_"
ENV_FILES = ("../.env", "/var/lib/dtk/offsite.env")

logger = logging.getLogger(__name__)


def _names(suffix: str) -> AliasChoices:
    """The two environment names of one setting, the CAPTUA_ name first."""
    return AliasChoices(f"{NEW_PREFIX}{suffix}", f"{OLD_PREFIX}{suffix}")


def _is_old_name(key: str) -> bool:
    return key.upper().startswith(OLD_PREFIX)


class _NewNamesEnv(EnvSettingsSource):
    """The process environment without its DTK_ variables."""

    def _load_env_vars(self) -> Mapping[str, Optional[str]]:
        return {k: v for k, v in super()._load_env_vars().items() if not _is_old_name(k)}


class _OldNamesEnv(EnvSettingsSource):
    """Only the process environment's DTK_ variables."""

    def _load_env_vars(self) -> Mapping[str, Optional[str]]:
        return {k: v for k, v in super()._load_env_vars().items() if _is_old_name(k)}


class _NewNamesDotEnv(DotEnvSettingsSource):
    """The env files without their DTK_ variables."""

    def _load_env_vars(self) -> Mapping[str, Optional[str]]:
        return {k: v for k, v in super()._load_env_vars().items() if not _is_old_name(k)}


class _OldNamesDotEnv(DotEnvSettingsSource):
    """Only the env files' DTK_ variables."""

    def _load_env_vars(self) -> Mapping[str, Optional[str]]:
        return {k: v for k, v in super()._load_env_vars().items() if _is_old_name(k)}


class Settings(BaseSettings):
    # Anything outside _DEV_ENVIRONMENTS requires a real SECRET_KEY
    APP_ENV: str = "development"
    DATABASE_USER: str = "user"
    DATABASE_PASSWORD: str = "password"
    DATABASE_HOST: str = "db"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "digitization_toolkit"
    DATA_DIR: str = Field(default="/var/lib/dtk", validation_alias=_names("DATA_DIR"))
    LOG_DIR: str = Field(default="/var/log/dtk", validation_alias=_names("LOG_DIR"))
    # Level for the backend's own loggers (`app.*`, `capture.*`) on stderr,
    # which the journal records on the appliance (app.core.logging_setup).
    # LOG_DIR above is something else: the rotating capture logs' folder.
    LOG_LEVEL: str = Field(default="INFO", validation_alias=_names("LOG_LEVEL"))
    
    PROJECTS_ROOT: str = ""
    CAMERA_BACKEND: str = "picamera2"
    SECRET_KEY: str = "dev-secret-change-me"
    # Per-unit token protecting first-user admin bootstrap over the network
    BOOTSTRAP_TOKEN: str = ""
    ACCESS_TOKEN_EXPIRE_SECONDS: int = 28800  # 8 hours
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]
    
    EXPORTS_ROOT: str = Field(default="", validation_alias=_names("EXPORTS_DIR"))
    # Application-level cap on uploaded image size. Defends the SD even if nginx (client_max_body_size 100m) is not in front of the app. Enforced while streaming.
    MAX_UPLOAD_BYTES: int = Field(default=100 * 1024 * 1024, validation_alias=_names("MAX_UPLOAD_BYTES"))

    # Offsite catalogue backup (NEH-249). Empty OFFSITE_REMOTE disables the
    # whole path, so a unit without provisioned credentials does nothing rather
    # than failing a timer every half hour. The remote name is an rclone remote
    # plus bucket, e.g. "b2unit:neogranadina-rionegro"; the scoped key behind it
    # is write + list only, which is why the upload skips the destination check.
    OFFSITE_REMOTE: str = Field(default="", validation_alias=_names("OFFSITE_REMOTE"))
    # The catalogue dump's directory under the tenant prefix:
    # <remote>/<tenant>/<prefix>/ (app.core.catalogue_backup.remote_dir).
    OFFSITE_DB_PREFIX: str = Field(default="db-backups", validation_alias=_names("OFFSITE_DB_PREFIX"))
    # The tenant prefix image objects are keyed under, relative to
    # OFFSITE_REMOTE: `<tenant>/<stem>/captures/<file>` (contract 1.1 §4).
    # Empty disables image upload, as an empty OFFSITE_REMOTE does
    # (app.core.image_upload), and the catalogue backup with it, whose dump
    # goes under the same prefix. Read from offsite.env like the keys above.
    OFFSITE_TENANT: str = Field(default="", validation_alias=_names("OFFSITE_TENANT"))
    # A second rclone remote, with the same layout as OFFSITE_REMOTE (remote
    # plus bucket), whose key may read objects under the tenant prefix. Only
    # restoring a freed volume reads through it (app.core.offsite_free); the
    # upload key stays write + list. Empty: restore is unavailable and its
    # route answers 409.
    OFFSITE_READ_REMOTE: str = Field(default="", validation_alias=_names("OFFSITE_READ_REMOTE"))
    # The age X25519 public key (age1...) every catalogue dump is encrypted to
    # before upload (app.core.catalogue_backup). Unset or invalid: no dump is
    # uploaded. The unit holds only this public half, never the private key.
    OFFSITE_DUMP_RECIPIENT: str = Field(default="", validation_alias=_names("OFFSITE_DUMP_RECIPIENT"))
    # The interim push on the Rionegro unit has run at 750 KB/s since 2026-08-20
    # over a ~12 Mbps link without starving the UI; its settings are the defaults.
    OFFSITE_BWLIMIT: str = Field(default="750k", validation_alias=_names("OFFSITE_BWLIMIT"))
    RCLONE_BIN: str = Field(default="rclone", validation_alias=_names("RCLONE_BIN"))
    RCLONE_CONFIG: str = Field(default="", validation_alias=_names("RCLONE_CONFIG"))
    # Overrides the default `docker compose exec -T db pg_dumpall` argv. Needed
    # anywhere the appliance's compose overlay is not one directory up — a dev
    # box, a container, or a host with a real postgres client on PATH.
    PG_DUMP_COMMAND: str = Field(default="", validation_alias=_names("PG_DUMP_COMMAND"))
    # Matches the interim timer: first attempt shortly after boot, then every
    # half hour while the unit is powered.
    OFFSITE_BACKUP_DELAY_SECONDS: int = Field(default=300, validation_alias=_names("OFFSITE_BACKUP_DELAY"))
    OFFSITE_BACKUP_INTERVAL_SECONDS: int = Field(default=1800, validation_alias=_names("OFFSITE_BACKUP_INTERVAL"))
    # While this file exists, nothing is sent offsite: the image upload, the
    # catalogue dump and freeing stop (app.core.offsite_hold), and each tick
    # reads it again, so creating or removing it needs no restart. An upgrade
    # creates it before writing the tenant to offsite.env and removes it after
    # the one-time commands (adopt, mark-freed, ...). Empty: offsite-hold in
    # DATA_DIR, /var/lib/dtk/offsite-hold on a unit.
    OFFSITE_HOLD_FILE: str = Field(default="", validation_alias=_names("OFFSITE_HOLD_FILE"))

    # Fisqua, which the unit sends each handed-over volume's capture manifest
    # to and asks for its volumes' status (app.core.fisqua_outbox, contract
    # 1.2). FISQUA_ENV is `production` or `staging`, and picks the host
    # `<tenant>.fisqua.org` or `<tenant>.staging.fisqua.org`, with the
    # tenant from OFFSITE_TENANT (app.core.fisqua_contract). Empty: not
    # configured, and nothing is queued or sent. FISQUA_BASE_URL replaces
    # that host, for tests and development only. Every call is the unit's,
    # over HTTPS, with this unit's token, a SecretStr so that printing the
    # settings never shows it; it is never logged and never returned by an API.
    FISQUA_ENV: str = Field(default="", validation_alias=_names("FISQUA_ENV"))
    FISQUA_BASE_URL: str = Field(default="", validation_alias=_names("FISQUA_BASE_URL"))
    FISQUA_TOKEN: SecretStr = Field(default=SecretStr(""), validation_alias=_names("FISQUA_TOKEN"))
    # A send that fails is retried after FISQUA_RETRY_BASE_SECONDS, then
    # twice as long after each further failure, never longer than
    # FISQUA_RETRY_MAX_SECONDS; a Retry-After from Fisqua is a floor. Sends
    # run after each image upload run (OFFSITE_BACKUP_INTERVAL_SECONDS),
    # so that interval is the finest step.
    FISQUA_RETRY_BASE_SECONDS: int = Field(default=300, validation_alias=_names("FISQUA_RETRY_BASE"))
    FISQUA_RETRY_MAX_SECONDS: int = Field(default=6 * 3600, validation_alias=_names("FISQUA_RETRY_MAX"))
    # Status is polled on its own schedule, every ten minutes while any sent
    # volume is not in a final state.
    FISQUA_POLL_INTERVAL_SECONDS: int = Field(default=600, validation_alias=_names("FISQUA_POLL_INTERVAL"))
    # Read and write timeout of each call; connecting has ten seconds.
    FISQUA_TIMEOUT_SECONDS: float = Field(default=60.0, validation_alias=_names("FISQUA_TIMEOUT"))
    # On: each image in a capture manifest carries `rotateDeg`, its recorded
    # rotate_deg (clockwise from the stored pixels, EXIF ignored). Off: no
    # image carries it. The manifest declares contract 1.6 either way
    # (app.core.capture_manifest). Fisqua staging reads rotateDeg; production
    # does not yet, and drops a key it does not know, so this stays off for
    # production until Fisqua reads it there.
    FISQUA_ROTATE_DEG: bool = Field(default=False, validation_alias=_names("FISQUA_ROTATE_DEG"))

    # APP_VERSION: overrides the release read from the superproject's VERSION
    # file (app.core.version). Unset on a unit; the file is the source.
    app_version: Optional[str] = None

    model_config = SettingsConfigDict(
        # Two files, and the LATER one wins on a conflict. Verified on the pinned
        # pydantic-settings 2.7.0: both are read, a key in the second overrides
        # the first, and a missing file in the tuple is tolerated silently.
        #
        # The split exists because generate-secrets.sh rotates DATABASE_PASSWORD
        # and republishes .env per unit. A provisioner holding an older snapshot
        # of that file could restore the old password with every key name still
        # matching, so every check would pass and the next start could not
        # authenticate. Two writers to one file cannot be made safe from inside
        # one of them, so provisioning owns a file of its own.
        #
        # offsite.env is written WHOLE by scripts/provision-rclone.sh alone, and
        # holds only the OFFSITE_* and RCLONE_* keys, and the Fisqua keys
        # FISQUA_TOKEN and FISQUA_ENV, each under its CAPTUA_ or DTK_ name.
        # Absent on a unit that has never been provisioned, which is why a
        # missing file must not be an error.
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore"  # Ignore extra fields from .env like uvicorn_host
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Earlier sources win. Each source is split in two, so that a CAPTUA_
        # name set anywhere wins over a DTK_ name set anywhere, and within each
        # family the process environment still wins over the files, and
        # offsite.env over ../.env. Names without a prefix
        # (DATABASE_*, SECRET_KEY, ...) are in the first pair only.
        env_file = getattr(dotenv_settings, "env_file", ENV_FILES)
        encoding = getattr(dotenv_settings, "env_file_encoding", "utf-8")
        return (
            init_settings,
            _NewNamesEnv(settings_cls),
            _NewNamesDotEnv(settings_cls, env_file=env_file, env_file_encoding=encoding),
            _OldNamesEnv(settings_cls),
            _OldNamesDotEnv(settings_cls, env_file=env_file, env_file_encoding=encoding),
            file_secret_settings,
        )
    
    @property
    def data_dir(self) -> Path:
        return Path(self.DATA_DIR)
    
    @property
    def log_dir(self) -> Path:
        return Path(self.LOG_DIR)
    
    @property
    def projects_dir(self) -> Path:
        # The active projects root, for reading. An override file that cannot be
        # parsed is treated as no override, so the default root is used (and
        # GET /system/storage reports override_invalid). An override that can be
        # parsed is used as it is, even when its path does not exist or is not
        # on a mounted drive: falling back to the SD card there would split a
        # project across two disks. Writers get their root from
        # storage_override.writable_projects_dir, which refuses such a path.
        from app.core.storage_override import get_storage_override_or_fallback
        override, _corrupt = get_storage_override_or_fallback()
        if override:
            return Path(override)
        return self.default_projects_dir

    @property
    def default_projects_dir(self) -> Path:
        """The projects root when no storage override is in effect."""
        return Path(self.PROJECTS_ROOT) if self.PROJECTS_ROOT else (self.data_dir / "projects")
    
    @property
    def offsite_hold_file(self) -> Path:
        return Path(self.OFFSITE_HOLD_FILE) if self.OFFSITE_HOLD_FILE else (self.data_dir / "offsite-hold")

    @property
    def exports_dir(self) -> Path:
        return Path(self.EXPORTS_ROOT) if self.EXPORTS_ROOT else (self.data_dir / "exports")


# Blacklist of SECRET_KEY values that must never sign session tokens outside development
_INSECURE_SECRET_KEYS = {
    "",
    "dev-secret-change-me",
    "secret-key-here-change-in-production",
    # Shipped in .env.example until 2026-07-25; units provisioned then may still carry it.
    "your-secret-key-here-change-in-production",
}

# Environments exempt from the production guards; anything else must set real secrets
_DEV_ENVIRONMENTS = {"dev", "development", "test", "testing", "local"}


def is_dev_env(app_env: str) -> bool:
    """True when APP_ENV names a development or test environment."""
    return app_env.strip().lower() in _DEV_ENVIRONMENTS


# Blacklist of DATABASE_PASSWORD values that must never reach a production DB outside dev
_INSECURE_DB_PASSWORDS = {
    "",
    "password",
    "change-this-in-production",
}


def _guard_secret_key(config: "Settings") -> None:
    """Refuse to start outside dev with a default/empty SECRET_KEY.

    Runs at import time, right after Settings() is built, so it fires before
    security.py reads settings.SECRET_KEY at its own module level. A FastAPI
    startup event would be too late: by then the weak key is already bound.
    """
    if config.APP_ENV.strip().lower() in _DEV_ENVIRONMENTS:
        return
    if config.SECRET_KEY.strip() in _INSECURE_SECRET_KEYS:
        raise RuntimeError(
            "SECRET_KEY is unset or left at the insecure default while "
            f"APP_ENV={config.APP_ENV!r}. Set a strong, unique SECRET_KEY "
            "(e.g. `openssl rand -hex 32`) before starting outside development."
        )


def _guard_database_password(config: "Settings") -> None:
    """Refuse to start outside dev with a placeholder DATABASE_PASSWORD.

    Per-unit passwords are generated at first boot; this is the
    defense-in-depth half: a unit that slipped through on a shared placeholder
    fails loudly instead of running with a credential known to every appliance.
    """
    if config.APP_ENV.strip().lower() in _DEV_ENVIRONMENTS:
        return
    if config.DATABASE_PASSWORD.strip() in _INSECURE_DB_PASSWORDS:
        raise RuntimeError(
            f"DATABASE_PASSWORD is unset or left at a shared placeholder while APP_ENV={config.APP_ENV!r}. Set a strong, unique DATABASE_PASSWORD (e.g. with `openssl rand -hex 32`) before starting outside development."
        )


def aliased_suffixes() -> list[str]:
    """The <X> of every setting read as CAPTUA_<X> or DTK_<X>."""
    suffixes = []
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            for choice in alias.choices:
                if isinstance(choice, str) and choice.startswith(NEW_PREFIX):
                    suffixes.append(choice[len(NEW_PREFIX):])
    return suffixes


def _environment_variables(env_files=ENV_FILES) -> dict[str, str]:
    """Every variable the settings could read, by upper-case name: the env
    files in order, then the process environment over them."""
    merged: dict[str, str] = {}
    for env_file in env_files:
        path = Path(env_file).expanduser()
        if path.is_file():
            for key, value in dotenv_values(path, encoding="utf-8").items():
                if value is not None:
                    merged[key.upper()] = value
    for key, value in os.environ.items():
        merged[key.upper()] = value
    return merged


def environment_names_report(env_files=ENV_FILES) -> dict[str, list[str]]:
    """Which names the settings were given under, without any value.

    `new` and `old` list the CAPTUA_ and DTK_ names that are set; `conflicts`
    lists each <X> whose two names are both set to different values, where
    the CAPTUA_ value is the one used.
    """
    variables = _environment_variables(env_files)
    report: dict[str, list[str]] = {"new": [], "old": [], "conflicts": []}
    for suffix in aliased_suffixes():
        new, old = f"{NEW_PREFIX}{suffix}", f"{OLD_PREFIX}{suffix}"
        # Set counts even when empty: an empty CAPTUA_ value is still the one used.
        if new in variables:
            report["new"].append(new)
        if old in variables:
            report["old"].append(old)
        if new in variables and old in variables and variables[new] != variables[old]:
            report["conflicts"].append(suffix)
    return report


def log_environment_names(env_files=ENV_FILES) -> dict[str, list[str]]:
    """Log once, at startup, which family of names the settings came from,
    and warn for each setting whose two names disagree. Names only, never
    values, so a secret such as the Fisqua token never reaches the journal."""
    report = environment_names_report(env_files)
    new, old = report["new"], report["old"]
    if new and old:
        logger.info(
            "Settings read under CAPTUA_ names (%s) and under the older DTK_ names (%s)",
            ", ".join(new), ", ".join(old),
        )
    elif old:
        logger.info("Settings read under the older DTK_ names (%s); CAPTUA_ names replace them", ", ".join(old))
    elif new:
        logger.info("Settings read under CAPTUA_ names (%s)", ", ".join(new))
    else:
        logger.info("No CAPTUA_ or DTK_ settings are set; every such setting has its default")
    for suffix in report["conflicts"]:
        logger.warning(
            "%s%s and %s%s are both set, to different values; %s%s is used",
            NEW_PREFIX, suffix, OLD_PREFIX, suffix, NEW_PREFIX, suffix,
        )
    return report


settings = Settings()
_guard_secret_key(settings)
_guard_database_password(settings)
