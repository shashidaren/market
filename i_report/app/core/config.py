from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "Intelligence Report Engine"
    APP_ENV: str = "production"

    DATABASE_URL: str
    REDIS_URL: str

    REPORT_DIR: Path = Path("/opt/i_report/reports")
    DATA_DIR: Path = Path("/opt/i_report/data")
    LOG_DIR: Path = Path("/opt/i_report/logs")

    DEFAULT_MARKET: str = "MY"
    DEFAULT_CURRENCY: str = "MYR"

    TELEGRAM_ENABLED: bool = False
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8088

    model_config = SettingsConfigDict(
        env_file="/opt/i_report/.env",
        case_sensitive=False,
    )


settings = Settings()

settings.REPORT_DIR.mkdir(parents=True, exist_ok=True)
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
