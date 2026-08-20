"""Thin Traccar REST client + provisioning helpers.

Used by the admin app to auto-create a Traccar device per Tracker
(`uniqueId = Tracker.device_key`) and to build the Traccar Client
provisioning URL/QR.
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
        device_group: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        # Every device this client creates joins this group. Traccar scopes the
        # device list per user, and being an administrator does not change that,
        # so admin visibility is one group link per user instead of one device
        # link per user per device.
        self._device_group = device_group
        self._group_id: int | None = None
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
            transport=self._transport,
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

    async def get_group(self, name: str) -> dict | None:
        """Return the device group with the given name, or None if absent."""
        async with self._client() as client:
            resp = await client.get("/api/groups")
            resp.raise_for_status()
            groups = resp.json()
        for group in groups:
            if group.get("name") == name:
                return group
        return None

    async def ensure_group(self, name: str) -> int:
        """Idempotently ensure the device group exists; return its id.

        Memoised on the client: the group is created once and never renamed, so
        a single lookup per process is enough.
        """
        if self._group_id is not None:
            return self._group_id
        group = await self.get_group(name)
        if group is None:
            async with self._client() as client:
                resp = await client.post("/api/groups", json={"name": name})
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError:
                    # Lost a create race with another worker.
                    group = await self.get_group(name)
                    if group is None:
                        raise
                else:
                    group = resp.json()
        self._group_id = group["id"]
        return self._group_id

    async def create_device(
        self, name: str, unique_id: str, *, group_id: int | None = None
    ) -> dict:
        """Create a device. Raises httpx.HTTPStatusError on failure."""
        payload: dict = {"name": name, "uniqueId": unique_id}
        if group_id is not None:
            payload["groupId"] = group_id
        async with self._client() as client:
            resp = await client.post("/api/devices", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def delete_device(self, unique_id: str) -> bool:
        """Delete the device with this uniqueId. True if one was there.

        Traccar has no delete-by-uniqueId, so the id is looked up first. A
        device that is already gone is not a failure: the caller is retiring a
        credential, and "no device holds it" is the state they asked for.
        """
        device = await self.get_device(unique_id)
        if device is None:
            return False
        async with self._client() as client:
            resp = await client.delete(f"/api/devices/{device['id']}")
            resp.raise_for_status()
        return True

    async def ensure_device(self, name: str, unique_id: str) -> dict:
        """Idempotently ensure a device exists for uniqueId; return it."""
        existing = await self.get_device(unique_id)
        if existing is not None:
            return existing
        group_id = None
        if self._device_group:
            try:
                group_id = await self.ensure_group(self._device_group)
            except httpx.HTTPError:
                # Grouping is a visibility convenience; never block provisioning
                # on it. The device is still created, just ungrouped.
                logger.warning(
                    "could not resolve Traccar group %r; creating %r ungrouped",
                    self._device_group,
                    unique_id,
                )
        try:
            return await self.create_device(name, unique_id, group_id=group_id)
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
        device_group=settings.traccar_device_group,
    )


TRACCAR_CLIENT_SCHEME = "org.traccar.client://config"


def build_config_url(device_key: str, settings: Settings | None = None) -> str:
    """Build the Traccar Client provisioning URL for a tracker.

    Shape: ``org.traccar.client://config?url={server}&id={device_key}&{profile}``,
    where ``server`` is the phone-reachable Traccar endpoint the client posts to.
    The ``id`` is ``Tracker.device_key``, the secret credential (never exposed in
    a feed), not the tracker's surrogate primary key.
    """
    settings = settings or get_settings()
    server = settings.traccar_client_base.rstrip("/")
    return (
        f"{TRACCAR_CLIENT_SCHEME}?url={server}"
        f"&id={quote(device_key, safe='')}&{settings.traccar_default_profile}"
    )


def qr_svg(data: str, *, scale: int = 4) -> str:
    """Return an inline SVG string encoding ``data`` as a QR code."""
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=scale, border=2)
    return buf.getvalue().decode("utf-8")


async def provision_device(nickname: str, device_key: str) -> None:
    """Create the Traccar device a new tracker will report through.

    Best-effort: the tracker row is already committed, and Traccar being down
    must not turn a successful create into a 500. A device that never appeared
    can be made later by re-provisioning, and the credential is unchanged.
    """
    try:
        await get_traccar_client().ensure_device(name=nickname, unique_id=device_key)
    except Exception:
        # No device_key in the message: this lands in logs.
        logger.warning(
            "could not provision the Traccar device for tracker %r",
            nickname,
            exc_info=True,
        )


async def retire_device(device_key: str) -> None:
    """Delete the Traccar device a deleted tracker was provisioning.

    Best-effort, exactly like the create side: the row is already gone, and a
    Traccar that is down must not turn a successful delete into a 500. What is
    left behind is a device whose ``uniqueId`` no longer maps to a tracker, so
    a fix posted with it is dropped rather than published.
    """
    try:
        await get_traccar_client().delete_device(device_key)
    except Exception:
        # No device_key in the message: this lands in logs.
        logger.warning(
            "could not retire the Traccar device for a tracker", exc_info=True
        )
