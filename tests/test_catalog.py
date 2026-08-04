"""The public feed catalog, over ASGI.

The first tests against the public app. ``ASGITransport`` never runs the
lifespan, so nothing sets ``app.state.redis``; the fake below stands in for it,
implementing only the three calls the router makes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker

from tests.factories import make_feed, make_user

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlmodel.ext.asyncio.session import AsyncSession

    type ClientFactory = Callable[[FakeRedis], Awaitable[AsyncClient]]


class FakeRedis:
    """A dict with the three methods ``routers/catalog.py`` calls.

    Values are bytes, because the real client is opened without
    ``decode_responses``, which is exactly the detail a stringly-typed fake
    would let a regression through on.
    """

    def __init__(self, data: dict[str, bytes] | None = None) -> None:
        self.data = data or {}

    async def scan_iter(
        self, match: str = "*", count: int | None = None
    ) -> AsyncGenerator[bytes]:
        prefix = match.removesuffix("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key.encode()

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        return [self.data.get(k) for k in keys]

    async def exists(self, key: str) -> int:
        return int(key in self.data)


def vehicle(trip_id: str, start_date: str | None = None) -> bytes:
    record: dict[str, object] = {
        "trip_id": trip_id,
        "lat": 42.0,
        "lon": -71.0,
        "timestamp": 0,
    }
    if start_date:
        record["start_date"] = start_date
    return json.dumps(record).encode()


@pytest.fixture
async def make_client(
    engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[ClientFactory]:
    """Builds a client over the public app, with the fake Redis of your choice."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@db/unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")

    import cafe_car.database as database
    from cafe_car.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(database, "_engine", engine)

    from cafe_car.main import create_public_app

    clients: list[AsyncClient] = []

    async def build(redis: FakeRedis) -> AsyncClient:
        app = create_public_app()
        app.state.redis = redis
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client

    yield build

    for client in clients:
        await client.aclose()
    get_settings.cache_clear()


async def test_a_feed_with_a_live_vehicle_reports_vehicles(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    owner = await make_user(session)
    feed = await make_feed(session, owner, "live-feed")
    session.add(Tracker(id="gently-tender-oyster", nickname="Bus", feed_id=feed.id))
    await session.commit()

    client = await make_client(
        FakeRedis({"vehicle:gently-tender-oyster:trip-1": vehicle("trip-1")})
    )
    response = await client.get("/feeds")

    assert response.status_code == 200
    (entry,) = response.json()
    assert entry["feed_name"] == "live-feed"
    assert entry["has_vehicles"] is True
    # No trip_update key exists, so the vehicle alone must not imply one.
    assert entry["has_trip_updates"] is False
    assert entry["has_alerts"] is False


async def test_a_feed_whose_tracker_has_gone_quiet_reports_nothing(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    """The 60s TTL is the freshness check: an absent key *is* a dead tracker."""
    owner = await make_user(session)
    feed = await make_feed(session, owner, "quiet-feed")
    session.add(Tracker(id="quietly-brave-otter", nickname="Bus", feed_id=feed.id))
    await session.commit()

    client = await make_client(FakeRedis({"vehicle:somebody-elses-tracker:t": b"{}"}))
    (entry,) = (await client.get("/feeds")).json()

    assert entry["has_vehicles"] is False
    assert entry["has_trip_updates"] is False


async def test_trip_updates_are_found_through_the_live_vehicle(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    """Including the start_date suffix, which a >24h trip instance carries."""
    owner = await make_user(session)
    feed = await make_feed(session, owner, "tu-feed")
    session.add(Tracker(id="boldly-sleepy-crane", nickname="Bus", feed_id=feed.id))
    await session.commit()

    client = await make_client(
        FakeRedis(
            {
                "vehicle:boldly-sleepy-crane:trip-1:20250103": vehicle(
                    "trip-1", "20250103"
                ),
                "trip_update:trip-1:20250103": b"{}",
            }
        )
    )
    (entry,) = (await client.get("/feeds")).json()

    assert entry["has_vehicles"] is True
    assert entry["has_trip_updates"] is True


async def test_an_expired_or_entityless_alert_is_not_reported(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    """Both filters that ``service_alerts.pb`` applies have to apply here too,
    or the catalog advertises a feed whose alerts endpoint is empty."""
    owner = await make_user(session)
    expired = await make_feed(session, owner, "expired-alert-feed")
    bare = await make_feed(session, owner, "bare-alert-feed")
    live = await make_feed(session, owner, "live-alert-feed")
    now = datetime.now(UTC)

    gone = ServiceAlert(
        feed_id=expired.id,
        header_text="Over",
        description_text="Finished.",
        active_period_end=now - timedelta(hours=1),
    )
    entityless = ServiceAlert(
        feed_id=bare.id, header_text="Selects nothing", description_text="."
    )
    current = ServiceAlert(
        feed_id=live.id, header_text="Bridge is out", description_text="Delays."
    )
    session.add_all([gone, entityless, current])
    await session.flush()
    session.add(InformedEntity(service_alert_id=gone.id, route_id="r1"))
    session.add(InformedEntity(service_alert_id=current.id, route_id="r1"))
    await session.commit()

    client = await make_client(FakeRedis())
    by_name = {e["feed_name"]: e for e in (await client.get("/feeds")).json()}

    assert by_name["expired-alert-feed"]["has_alerts"] is False
    assert by_name["bare-alert-feed"]["has_alerts"] is False
    assert by_name["live-alert-feed"]["has_alerts"] is True


async def test_the_catalog_does_not_leak_the_tracker_credential(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    """``Tracker.id`` is the Traccar credential. The catalog is world-readable."""
    owner = await make_user(session)
    feed = await make_feed(session, owner, "secret-feed")
    session.add(
        Tracker(id="wildly-mellow-heron", nickname="Riverliner", feed_id=feed.id)
    )
    await session.commit()

    client = await make_client(
        FakeRedis({"vehicle:wildly-mellow-heron:trip-1": vehicle("trip-1")})
    )
    response = await client.get("/feeds")

    assert "wildly-mellow-heron" not in response.text
    assert "Riverliner" not in response.text


async def test_the_urls_are_the_public_rt_ones(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    owner = await make_user(session)
    await make_feed(session, owner, "url-feed")

    client = await make_client(FakeRedis())
    response = await client.get("/feeds")
    (entry,) = response.json()

    assert entry["static_url"] == "https://example.com/gtfs.zip"
    assert entry["vehicle_positions_url"] == (
        "https://rt.gtfs.zone/url-feed/vehicle_positions.pb"
    )
    assert entry["trip_updates_url"] == "https://rt.gtfs.zone/url-feed/trip_updates.pb"
    assert entry["service_alerts_url"] == (
        "https://rt.gtfs.zone/url-feed/service_alerts.pb"
    )
    assert response.headers["cache-control"] == "public, max-age=30"


async def test_the_catalog_needs_no_proxy_gymnastics(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    """``internal.py::feed_urls`` 403s anything carrying X-Forwarded-For, which
    is every request that reached us through Traefik. This one is public."""
    owner = await make_user(session)
    await make_feed(session, owner, "public-feed")

    client = await make_client(FakeRedis())
    response = await client.get("/feeds", headers={"X-Forwarded-For": "1.2.3.4"})

    assert response.status_code == 200
    assert len(response.json()) == 1
