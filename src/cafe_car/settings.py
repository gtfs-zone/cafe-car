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
    cors_allowed_origins: list[str] = []

    # Shared bearer token guarding the service-to-service ingest API
    # (/ingest/*). Producers (hell-gate-bridge, simulate_trip.py) send it as
    # `Authorization: Bearer <token>`. Separate secret from the Traccar token.
    ingest_api_token: str | None = None

    # Traccar integration (device provisioning).
    traccar_url: str = "http://traccar:8082"
    # Auth: prefer a bearer API token; fall back to Basic auth (email/password).
    traccar_api_token: str | None = None
    traccar_email: str | None = None
    traccar_password: str | None = None
    # Phone-reachable Traccar endpoint the Traccar Client posts to (osmand :5055).
    # Emitted as the `url=` param of the org.traccar.client://config deep link, so
    # it must be reachable from the driver's phone, not just the host.
    traccar_client_base: str = "http://localhost:5055"
    # Single global tracking profile baked into every provisioning QR/URL.
    traccar_default_profile: str = (
        "accuracy=highest&distance=1&interval=30"
        "&heartbeat=3000&wakelock=true&stop_detection=true"
    )

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
