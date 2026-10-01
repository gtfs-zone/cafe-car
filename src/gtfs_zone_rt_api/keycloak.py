"""Thin read-only Keycloak admin-API client.

Used by the account page to answer "which upstream providers can I sign in
with?". That is Keycloak's question to answer, not ours: an `Identity` row is
one *realm account*, and a login only ever reports the single broker it came
through, so the local DB can never hold the full list.

Authenticates as a service account (client credentials) rather than reusing the
caller's token, which keeps this independent of how oauth2-proxy passes tokens
through, since that differs between debug and production.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache

import httpx

from gtfs_zone_rt_api.settings import get_settings

log = logging.getLogger(__name__)

# Renew this many seconds before the token actually expires, so a request never
# leaves with a token that dies in flight.
_EXPIRY_MARGIN = 30.0


class KeycloakClient:
    """The slice of the Keycloak admin API the account page needs.

    A fresh ``httpx.AsyncClient`` per call, matching ``gtfs_zone_rt_api.traccar``. The
    service-account token, though, is cached across calls: it is valid for
    minutes and re-fetching it would double every request.
    """

    def __init__(
        self,
        base_url: str,
        realm: str,
        *,
        client_id: str,
        client_secret: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._realm = realm
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)

    async def _access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token
        data = {"grant_type": "client_credentials", "client_id": self._client_id}
        if self._client_secret:
            data["client_secret"] = self._client_secret
        async with self._client() as client:
            resp = await client.post(
                f"/realms/{self._realm}/protocol/openid-connect/token", data=data
            )
            resp.raise_for_status()
            payload = resp.json()
        self._token = payload["access_token"]
        # `expires_in` is seconds; monotonic so a clock change cannot strand us
        # with a token we believe is valid forever.
        self._token_expires_at = time.monotonic() + max(
            float(payload.get("expires_in", 60)) - _EXPIRY_MARGIN, 0.0
        )
        return self._token

    async def _get(self, path: str) -> httpx.Response:
        token = await self._access_token()
        async with self._client() as client:
            return await client.get(path, headers={"Authorization": f"Bearer {token}"})

    async def subject_for_username(self, username: str) -> str | None:
        """The realm account's subject, or None when the realm has no such user.

        Exact-match, so a username that is a prefix of another cannot return the
        wrong account. Same read-only ``view-users`` role as the rest of this
        client.
        """
        resp = await self._get(
            f"/admin/realms/{self._realm}/users?username={username}&exact=true&max=2"
        )
        resp.raise_for_status()
        users = resp.json()
        if not users:
            return None
        return str(users[0]["id"])


@lru_cache
def get_keycloak_client() -> KeycloakClient | None:
    """The configured client, or None when the lookup is not configured."""
    settings = get_settings()
    if not settings.keycloak_client_id:
        return None
    return KeycloakClient(
        settings.keycloak_url,
        settings.keycloak_realm,
        client_id=settings.keycloak_client_id,
        client_secret=settings.keycloak_client_secret,
    )
