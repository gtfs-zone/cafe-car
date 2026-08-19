from functools import lru_cache

from pydantic import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: PostgresDsn
    redis_url: RedisDsn
    session_secret_key: str
    # Namespaces the `provider_subject` of an Identity. Keycloak is the issuer
    # oauth2-proxy talks to; a subject is only meaningful within one issuer.
    oidc_provider: str = "keycloak"
    # Keycloak group whose members see and edit every feed, not just their own.
    # Matched against the flat `groups` claim, which Keycloak maps with
    # full.path=false, so this is the bare group name and not "/gtfs-admins".
    # The same group gates Traccar entirely (openid.allowGroup there).
    admin_group: str = "gtfs-admins"
    oauth2_proxy_logout_url: str = "/oauth2/sign_out"
    # Keycloak's self-serve Account Console, where a signed-in person adds
    # another provider. Keycloak owns linking, so it is also where linking is
    # undone. Empty hides the link.
    keycloak_account_url: str = ""
    # Read-only admin-API access, used by /account to report which upstream
    # providers are linked to the signed-in person's realm account. Keycloak is
    # the only place that knows: a login only ever tells us the *one* broker it
    # came through. A service account rather than the caller's own token, so
    # this does not depend on how oauth2-proxy passes tokens through. Needs the
    # realm-management `view-users` role and nothing more. Empty client id
    # disables the lookup and the page falls back to what it has seen itself.
    keycloak_url: str = "http://keycloak:8090"
    keycloak_realm: str = "gtfs"
    keycloak_client_id: str = ""
    keycloak_client_secret: str | None = None
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
    # Every auto-created device joins this Traccar group. Traccar scopes the
    # device list per user via tc_user_device and being an administrator does
    # NOT bypass that, so an admin sees nothing until something is shared with
    # them. Sharing the group once per user covers every device forever, instead
    # of one POST /api/permissions per device per user. Set empty to disable.
    traccar_device_group: str = "All Vehicles"
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
