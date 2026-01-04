from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://user:password@db:5432/digitization_toolkit"
    DATABASE_USER: str = "user"
    DATABASE_HOST: str = "db"
    DATABASE_PORT: int = 5432
    DATABASE_NAME: str = "digitization_toolkit"
    UVICORN_HOST: str = "0.0.0.0"
    UVICORN_PORT: int = 8000
    LOG_LEVEL: str = "info"
    DTK_DATA_DIR: str = "/var/lib/dtk"
    DTK_LOG_DIR: str = "/var/log/dtk"
    PROJECTS_ROOT: str = Field(default="", env="PROJECTS_ROOT")
    EXPORTS_ROOT: str = Field(default="", env="DTK_EXPORTS_DIR")

    model_config = {
        "env_file": ".env"
    }
    
    @property
    def data_dir(self) -> Path:
        return Path(self.DTK_DATA_DIR)
    
    @property
    def log_dir(self) -> Path:
        return Path(self.DTK_LOG_DIR)
    
    @property
    def projects_dir(self) -> Path:
        return Path(self.PROJECTS_ROOT) if self.PROJECTS_ROOT else (self.data_dir / "projects")
    
    @property
    def exports_dir(self) -> Path:
        return Path(self.EXPORTS_ROOT) if self.EXPORTS_ROOT else (self.data_dir / "exports")


settings = Settings()
