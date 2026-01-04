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
    PROJECTS_ROOT: str = Field(default="/var/lib/dtk/projects", env="PROJECTS_ROOT")

    model_config = {
        "env_file": ".env"
    }


settings = Settings()
