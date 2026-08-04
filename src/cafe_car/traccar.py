"""Thin Traccar REST client + provisioning helpers.

Used by the admin app to auto-create a Traccar device per Tracker
(`uniqueId = tracker id`) and to build the Traccar Client provisioning URL/QR.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache
from urllib.parse import quote

import httpx
import segno

from cafe_car.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class TraccarClient:
    """Minimal async client for the Traccar REST API.

    Authenticates with a bearer API token when configured, otherwise falls back
    to Basic auth (email/password), the dev default.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        email: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers: dict[str, str] = {}
        self._auth: httpx.BasicAuth | None = None
        if api_token:
            self._headers["Authorization"] = f"Bearer {api_token}"
        elif email and password:
            self._auth = httpx.BasicAuth(email, password)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            auth=self._auth,
            timeout=self._timeout,
        )

    async def get_device(self, unique_id: str) -> dict | None:
        """Return the device with the given uniqueId, or None if absent."""
        async with self._client() as client:
            resp = await client.get("/api/devices", params={"uniqueId": unique_id})
            resp.raise_for_status()
            devices = resp.json()
        for device in devices:
            if device.get("uniqueId") == unique_id:
                return device
        return None

    async def create_device(self, name: str, unique_id: str) -> dict:
        """Create a device. Raises httpx.HTTPStatusError on failure."""
        async with self._client() as client:
            resp = await client.post(
                "/api/devices", json={"name": name, "uniqueId": unique_id}
            )
            resp.raise_for_status()
            return resp.json()

    async def ensure_device(self, name: str, unique_id: str) -> dict:
        """Idempotently ensure a device exists for uniqueId; return it."""
        existing = await self.get_device(unique_id)
        if existing is not None:
            return existing
        try:
            return await self.create_device(name, unique_id)
        except httpx.HTTPStatusError:
            # Lost a create race, or Traccar rejected a duplicate uniqueId.
            existing = await self.get_device(unique_id)
            if existing is not None:
                return existing
            raise


@lru_cache
def get_traccar_client() -> TraccarClient:
    settings = get_settings()
    return TraccarClient(
        settings.traccar_url,
        api_token=settings.traccar_api_token,
        email=settings.traccar_email,
        password=settings.traccar_password,
    )


TRACCAR_CLIENT_SCHEME = "org.traccar.client://config"


def build_config_url(tracker_id: str, settings: Settings | None = None) -> str:
    """Build the Traccar Client provisioning URL for a tracker.

    Shape: ``org.traccar.client://config?url={server}&id={tracker_id}&{profile}``,
    where ``server`` is the phone-reachable Traccar endpoint the client posts to.
    The ``id`` is the tracker's secret credential (never exposed in a feed).
    """
    settings = settings or get_settings()
    server = settings.traccar_client_base.rstrip("/")
    return (
        f"{TRACCAR_CLIENT_SCHEME}?url={server}"
        f"&id={quote(tracker_id, safe='')}&{settings.traccar_default_profile}"
    )


def qr_svg(data: str, *, scale: int = 4) -> str:
    """Return an inline SVG string encoding ``data`` as a QR code."""
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=scale, border=2)
    return buf.getvalue().decode("utf-8")
