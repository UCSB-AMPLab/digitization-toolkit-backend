from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path

class Settings(BaseSettings):
    # Deployment environment
    # Anything outside _DEV_ENVIRONMENTS requires a real SECRET_KEY
    APP_ENV: str = "development"
    DATABASE_USER: str = "user"
    DATABASE_PASSWORD: str = "password"
    DATABASE_HOST: str = "db"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "digitization_toolkit"
    DTK_DATA_DIR: str = "/var/lib/dtk"
    DTK_LOG_DIR: str = "/var/log/dtk"
    
    PROJECTS_ROOT: str = ""
    CAMERA_BACKEND: str = "picamera2"
    SECRET_KEY: str = "dev-secret-change-me"
    # Per-unit token protecting first-user admin bootstrap over the network
    BOOTSTRAP_TOKEN: str = ""
    ACCESS_TOKEN_EXPIRE_SECONDS: int = 28800  # 8 hours
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]
    
    EXPORTS_ROOT: str = Field(default="", validation_alias="DTK_EXPORTS_DIR")
    # Application-level cap on uploaded image size. Defends the SD even if nginx (client_max_body_size 100m) is not in front of the app. Enforced while streaming.
    MAX_UPLOAD_BYTES: int = Field(default=100 * 1024 * 1024, validation_alias="DTK_MAX_UPLOAD_BYTES")

    # Offsite catalogue backup (NEH-249). Empty OFFSITE_REMOTE disables the
    # whole path, so a unit without provisioned credentials does nothing rather
    # than failing a timer every half hour. The remote name is an rclone remote
    # plus bucket, e.g. "b2unit:neogranadina-rionegro"; the scoped key behind it
    # is write + list only, which is why the upload skips the destination check.
    OFFSITE_REMOTE: str = Field(default="", validation_alias="DTK_OFFSITE_REMOTE")
    OFFSITE_DB_PREFIX: str = Field(default="db-backups", validation_alias="DTK_OFFSITE_DB_PREFIX")
    # The tenant prefix image objects are keyed under, relative to
    # OFFSITE_REMOTE: `<tenant>/<stem>/captures/<file>` (contract 1.1 §4).
    # Empty disables image upload, as an empty OFFSITE_REMOTE does
    # (app.core.image_upload). Read from offsite.env like the keys above.
    OFFSITE_TENANT: str = Field(default="", validation_alias="DTK_OFFSITE_TENANT")
    # The interim push on the Rionegro unit has run at 750 KB/s since 2026-08-20
    # over a ~12 Mbps link without starving the UI; its settings are the defaults.
    OFFSITE_BWLIMIT: str = Field(default="750k", validation_alias="DTK_OFFSITE_BWLIMIT")
    RCLONE_BIN: str = Field(default="rclone", validation_alias="DTK_RCLONE_BIN")
    RCLONE_CONFIG: str = Field(default="", validation_alias="DTK_RCLONE_CONFIG")
    # Overrides the default `docker compose exec -T db pg_dumpall` argv. Needed
    # anywhere the appliance's compose overlay is not one directory up — a dev
    # box, a container, or a host with a real postgres client on PATH.
    PG_DUMP_COMMAND: str = Field(default="", validation_alias="DTK_PG_DUMP_COMMAND")
    # Matches the interim timer: first attempt shortly after boot, then every
    # half hour while the unit is powered.
    OFFSITE_BACKUP_DELAY_SECONDS: int = Field(default=300, validation_alias="DTK_OFFSITE_BACKUP_DELAY")
    OFFSITE_BACKUP_INTERVAL_SECONDS: int = Field(default=1800, validation_alias="DTK_OFFSITE_BACKUP_INTERVAL")

    app_version: str = "0.0.0-dev"

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
        # holds only DTK_OFFSITE_* and DTK_RCLONE_* keys. Absent on a unit that
        # has never been provisioned, which is why a missing file must not be an
        # error.
        env_file=("../.env", "/var/lib/dtk/offsite.env"),
        env_file_encoding="utf-8",
        extra="ignore"  # Ignore extra fields from .env like uvicorn_host
    )
    
    @property
    def data_dir(self) -> Path:
        return Path(self.DTK_DATA_DIR)
    
    @property
    def log_dir(self) -> Path:
        return Path(self.DTK_LOG_DIR)
    
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
    def exports_dir(self) -> Path:
        return Path(self.EXPORTS_ROOT) if self.EXPORTS_ROOT else (self.data_dir / "exports")


# Blacklist of SECRET_KEY values that must never sign session tokens outside development
_INSECURE_SECRET_KEYS = {
    "",
    "dev-secret-change-me",
    "secret-key-here-change-in-production",
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


settings = Settings()
_guard_secret_key(settings)
_guard_database_password(settings)
