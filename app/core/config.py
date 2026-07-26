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
    ACCESS_TOKEN_EXPIRE_SECONDS: int = 28800  # 8 hours
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]
    
    EXPORTS_ROOT: str = Field(default="", validation_alias="DTK_EXPORTS_DIR")
    # Application-level cap on uploaded image size. Defends the SD even if nginx (client_max_body_size 100m) is not in front of the app. Enforced while streaming.
    MAX_UPLOAD_BYTES: int = Field(default=100 * 1024 * 1024, validation_alias="DTK_MAX_UPLOAD_BYTES")
    app_version: str = "0.0.0-dev"

    model_config = SettingsConfigDict(
        env_file="../.env",  # Load .env from project root when running from backend/
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
        from app.core.storage_override import get_storage_override
        override = get_storage_override()
        if override:
            return Path(override)
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
