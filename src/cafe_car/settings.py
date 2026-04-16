from functools import lru_cache

from pydantic import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: PostgresDsn
    redis_url: RedisDsn
    session_secret_key: str
    oidc_provider: str = "dex"
    oauth2_proxy_logout_url: str = "/oauth2/sign_out"
    debug: bool = False
    celery_broker_url: str = "redis://redis:6379/3"

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
