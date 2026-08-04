"""GTFS-RT protobuf endpoints, over ASGI.

Focused on `VehicleDescriptor.id` uniqueness: a producer that runs several
concurrent vehicles under one tracker credential but supplies no per-vehicle
`vehicle_id` must not have them collapse onto the bare tracker nickname.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from google.transit import gtfs_realtime_pb2
from httpx import ASGITransport, AsyncClient
from railroad_club.models.tracker import Tracker

from tests.factories import make_feed, make_user
from tests.test_catalog import FakeRedis as _CatalogFakeRedis

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlmodel.ext.asyncio.session import AsyncSession

    type ClientFactory = Callable[[FakeRedis], Awaitable[AsyncClient]]


class FakeRedis(_CatalogFakeRedis):
    """Adds the single-key ``get`` the gtfs_rt router calls (catalog.py only
    ever needs ``mget``/``scan_iter``/``exists``)."""

    async def get(self, key: str | bytes) -> bytes | None:
        if isinstance(key, bytes):
            key = key.decode()
        return self.data.get(key)


def vehicle_record(
    trip_id: str | None = None,
    start_date: str | None = None,
    vehicle_id: str | None = None,
    vehicle_label: str | None = None,
) -> bytes:
    record: dict[str, object] = {
        "lat": 42.0,
        "lon": -71.0,
        "timestamp": 0,
        "bearing": None,
        "speed": None,
    }
    if trip_id:
        record["trip_id"] = trip_id
    if start_date:
        record["start_date"] = start_date
    if vehicle_id:
        record["vehicle_id"] = vehicle_id
    if vehicle_label:
        record["vehicle_label"] = vehicle_label
    return json.dumps(record).encode()


@pytest.fixture
async def make_client(
    engine: AsyncEngine,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[ClientFactory]:
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


def _parse_vehicles(content: bytes) -> gtfs_realtime_pb2.FeedMessage:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.ParseFromString(content)
    return msg


async def test_two_concurrent_vehicles_under_one_tracker_get_distinct_ids(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    """The exact buswhere bug: one tracker, two live devices, neither carrying
    a producer-supplied `vehicle_id`; the feed must still not reuse a
    VehicleDescriptor.id across them."""
    owner = await make_user(session)
    feed = await make_feed(session, owner, "cc-feed")
    session.add(Tracker(id="ccbus", nickname="CC Bus", feed_id=feed.id))
    await session.commit()

    client = await make_client(
        FakeRedis(
            {
                "vehicle:ccbus:shopping-trip:20260803": vehicle_record(
                    "shopping-trip", "20260803"
                ),
                "vehicle:ccbus:HUD_ALB_B_PM_NB:20260803": vehicle_record(
                    "HUD_ALB_B_PM_NB", "20260803"
                ),
            }
        )
    )
    response = await client.get("/cc-feed/vehicle_positions.pb")
    assert response.status_code == 200

    msg = _parse_vehicles(response.content)
    ids = [e.vehicle.vehicle.id for e in msg.entity]
    assert len(ids) == 2
    assert len(set(ids)) == 2, f"vehicle.id collided: {ids}"
    assert set(ids) == {
        "CC Bus:shopping-trip:20260803",
        "CC Bus:HUD_ALB_B_PM_NB:20260803",
    }


async def test_a_producer_supplied_vehicle_id_is_still_honored(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    owner = await make_user(session)
    feed = await make_feed(session, owner, "amtrak-feed")
    session.add(Tracker(id="amtrak-cred", nickname="Amtrak", feed_id=feed.id))
    await session.commit()

    client = await make_client(
        FakeRedis(
            {
                "vehicle:amtrak-cred:trip-1:20260803": vehicle_record(
                    "trip-1", "20260803", vehicle_id="53:20260803"
                ),
            }
        )
    )
    response = await client.get("/amtrak-feed/vehicle_positions.pb")
    msg = _parse_vehicles(response.content)
    (entity,) = msg.entity
    assert entity.vehicle.vehicle.id == "53:20260803"
    assert entity.id == "53:20260803"


async def test_trip_update_vehicle_id_is_also_deduplicated(
    make_client: ClientFactory, session: AsyncSession
) -> None:
    owner = await make_user(session)
    feed = await make_feed(session, owner, "tu-cc-feed")
    session.add(Tracker(id="ccbus2", nickname="CC Bus", feed_id=feed.id))
    await session.commit()

    client = await make_client(
        FakeRedis(
            {
                "vehicle:ccbus2:shopping-trip:20260803": vehicle_record(
                    "shopping-trip", "20260803"
                ),
                "vehicle:ccbus2:HUD_ALB_B_PM_NB:20260803": vehicle_record(
                    "HUD_ALB_B_PM_NB", "20260803"
                ),
                "trip_update:shopping-trip:20260803": json.dumps(
                    {"trip_id": "shopping-trip", "timestamp": 0}
                ).encode(),
                "trip_update:HUD_ALB_B_PM_NB:20260803": json.dumps(
                    {"trip_id": "HUD_ALB_B_PM_NB", "timestamp": 0}
                ).encode(),
            }
        )
    )
    response = await client.get("/tu-cc-feed/trip_updates.pb")
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.ParseFromString(response.content)

    ids = [e.trip_update.vehicle.id for e in msg.entity]
    assert len(ids) == 2
    assert len(set(ids)) == 2, f"vehicle.id collided: {ids}"
