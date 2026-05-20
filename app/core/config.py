from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict
from pathlib import Path

class Settings(BaseSettings):
    DATABASE_USER: str = "user"
    DATABASE_PASSWORD: str = "password"
    DATABASE_HOST: str = "db"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "digitization_toolkit"
    DTK_DATA_DIR: str = "/var/lib/dtk"
    DTK_LOG_DIR: str = "/var/log/dtk"
    PROJECTS_ROOT: str = Field(default="", env="PROJECTS_ROOT")
    EXPORTS_ROOT: str = Field(default="", env="DTK_EXPORTS_DIR")
    CAMERA_BACKEND: str = Field(default="picamera2", env="CAMERA_BACKEND")
    SECRET_KEY: str = Field(default="dev-secret-change-me", env="SECRET_KEY")
    ACCESS_TOKEN_EXPIRE_SECONDS: int = Field(default=28800, env="ACCESS_TOKEN_EXPIRE_SECONDS")  # 8 hours
    app_version: str = "0.0.0-dev"

    model_config = ConfigDict(
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


settings = Settings()
