"""Tracker positions: the authenticated read, and the push that keeps it fresh.

Two halves of one feature, so one file.

``GET /api/feeds/{id}/tracker-positions`` is the bootstrap: a client that has
just selected a feed has missed every fix already in flight. ``/ingest/position``
is the live half: each fix is published on the feed's channel as it lands.

What is asserted here and nowhere else:

* The endpoint is **scoped**. It answers with a fleet's live locations, which is
  exactly the thing a stranger must not be handed.
* The payload keys by the **surrogate** ``Tracker.id`` plus the producer's
  ``vehicle_id``, never the nickname and never ``device_key``. Two trackers
  sharing a nickname used to collapse onto a single map feature, and the
  credential must not travel in a payload that gets pushed, logged and dumped
  on a page.
* Ingest **publishes what the endpoint would have returned**. The two are built
  by one function, and a client stores both the same way, so a divergence would
  make a map that disagrees with itself depending on how it was populated.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient
from railroad_club.feed_events import feed_channel
from railroad_club.models.tracker import Tracker

from tests.factories import PROVIDER, make_feed, make_user

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlmodel.ext.asyncio.session import AsyncSession

    type ClientFactory = Callable[[FakeRedis], Awaitable[AsyncClient]]

INGEST_TOKEN = "ingest-token"


class FakeRedis:
    """The handful of methods the two routes call.

    Values are bytes, because the real client is opened without
    ``decode_responses``. ``published`` keeps every ``(channel, payload)`` pair
    rather than delivering it: what matters here is what went onto the channel,
    and ``test_events.py`` already covers what comes off one.
    """

    def __init__(self, data: dict[str, bytes] | None = None) -> None:
        self.data = data or {}
        self.published: list[tuple[str, str]] = []

    async def scan_iter(
        self, match: str = "*", count: int | None = None
    ) -> AsyncGenerator[bytes]:
        prefix = match.removesuffix("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key.encode()

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        return [self.data.get(k) for k in keys]

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def exists(self, key: str) -> int:
        return int(key in self.data)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.data[key] = value.encode()

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


def record(**overrides: object) -> bytes:
    base: dict[str, Any] = {
        "tracker_id": "gently-tender-oyster",
        "trip_id": "T1",
        "lat": 42.0,
        "lon": -71.0,
        "bearing": 90.0,
        "speed": 12.5,
        "timestamp": 1_700_000_000,
    }
    base.update(overrides)
    return json.dumps(base).encode()


def _headers(subject: str, email: str) -> dict:
    return {"X-Auth-Request-User": subject, "X-Auth-Request-Email": email}


OWNER = _headers("kc-owner", "owner@example.com")
STRANGER = _headers("kc-stranger", "stranger@example.com")


@pytest.fixture
async def make_admin_client(
    engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[ClientFactory]:
    """A client over the admin app, which is where ``/api`` is mounted."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@db/unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("OIDC_PROVIDER", PROVIDER)

    import cafe_car.database as database
    from cafe_car.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(database, "_engine", engine)

    from cafe_car.admin_main import create_admin_app

    clients: list[AsyncClient] = []

    async def build(redis: FakeRedis) -> AsyncClient:
        app = create_admin_app()
        # Nothing runs the lifespan under ASGITransport, so the client the
        # routes read off app.state is set by hand.
        app.state.redis = redis
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client

    yield build

    for client in clients:
        await client.aclose()
    get_settings.cache_clear()


@pytest.fixture
async def make_public_client(
    engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[ClientFactory]:
    """A client over the public app, which is where ``/ingest`` is mounted."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@db/unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("INGEST_API_TOKEN", INGEST_TOKEN)

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


@pytest.fixture
async def world(session: AsyncSession) -> dict:
    """One owner with a two-tracker feed, and a stranger with their own."""
    owner = await make_user(session, email="owner@example.com", subject="kc-owner")
    stranger = await make_user(
        session, email="stranger@example.com", subject="kc-stranger"
    )
    feed = await make_feed(session, owner, "owner-feed")
    other = await make_feed(session, stranger, "stranger-feed")

    reporting = Tracker(
        id="gently-tender-oyster",
        device_key="lively-happy-otter",
        nickname="Otter",
        feed_id=feed.id,
    )
    silent = Tracker(
        id="quietly-sleepy-heron",
        device_key="wildly-eager-badger",
        nickname="Heron",
        feed_id=feed.id,
    )
    session.add_all([reporting, silent])
    await session.commit()

    return {
        "owner": owner,
        "stranger": stranger,
        "feed": feed,
        "other": other,
        "reporting": reporting,
        "silent": silent,
    }


class TestTrackerPositions:
    async def test_a_stranger_cannot_read_a_feeds_positions(
        self, make_admin_client: ClientFactory, world: dict
    ) -> None:
        client = await make_admin_client(FakeRedis())

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/tracker-positions", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_returns_the_live_fleet_keyed_by_the_surrogate(
        self, make_admin_client: ClientFactory, world: dict
    ) -> None:
        client = await make_admin_client(
            FakeRedis({"vehicle:gently-tender-oyster": record()})
        )

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/tracker-positions", headers=OWNER
        )

        assert response.status_code == 200
        (vehicle,) = response.json()
        assert vehicle["key"] == "gently-tender-oyster"
        assert vehicle["trackerId"] == "gently-tender-oyster"
        assert vehicle["tripId"] == "T1"
        assert vehicle["lat"] == 42.0
        assert vehicle["bearing"] == 90.0
        # The nickname is what the vehicle is *labelled* with, never what it is
        # keyed by.
        assert vehicle["label"] == "Otter"

    async def test_a_tracker_with_no_fix_is_absent(
        self, make_admin_client: ClientFactory, world: dict
    ) -> None:
        client = await make_admin_client(
            FakeRedis({"vehicle:gently-tender-oyster": record()})
        )

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/tracker-positions", headers=OWNER
        )

        assert [v["trackerId"] for v in response.json()] == ["gently-tender-oyster"]

    async def test_another_feeds_vehicles_are_not_included(
        self, make_admin_client: ClientFactory, world: dict, session: AsyncSession
    ) -> None:
        session.add(
            Tracker(
                id="oddly-swift-marten",
                device_key="calmly-brave-lynx",
                nickname="Marten",
                feed_id=world["other"].id,
            )
        )
        await session.commit()
        client = await make_admin_client(
            FakeRedis(
                {
                    "vehicle:gently-tender-oyster": record(),
                    "vehicle:oddly-swift-marten": record(
                        tracker_id="oddly-swift-marten", trip_id="T9"
                    ),
                }
            )
        )

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/tracker-positions", headers=OWNER
        )

        assert [v["trackerId"] for v in response.json()] == ["gently-tender-oyster"]

    async def test_the_device_key_never_appears(
        self, make_admin_client: ClientFactory, world: dict
    ) -> None:
        client = await make_admin_client(
            FakeRedis({"vehicle:gently-tender-oyster": record()})
        )

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/tracker-positions", headers=OWNER
        )

        assert "lively-happy-otter" not in response.text

    async def test_concurrent_vehicles_on_one_tracker_stay_distinct(
        self, make_admin_client: ClientFactory, world: dict
    ) -> None:
        """One credential, several vehicles: the ``vehicle_id`` is what parts them.

        A key that were the tracker alone would collapse these onto one map
        feature, which the panel would then report as a single vehicle.
        """
        client = await make_admin_client(
            FakeRedis(
                {
                    "vehicle:gently-tender-oyster:449:20260101": record(
                        vehicle_id="449:20260101", start_date="20260101"
                    ),
                    "vehicle:gently-tender-oyster:449:20260102": record(
                        vehicle_id="449:20260102", start_date="20260102"
                    ),
                }
            )
        )

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/tracker-positions", headers=OWNER
        )

        vehicles = response.json()
        # A vehicle_id carrying its own colons survives the round trip, which is
        # what splitting on the first colon only is for.
        assert [v["key"] for v in vehicles] == [
            "gently-tender-oyster:449:20260101",
            "gently-tender-oyster:449:20260102",
        ]
        assert [v["trackerId"] for v in vehicles] == ["gently-tender-oyster"] * 2
        assert len({v["vehicleId"] for v in vehicles}) == 2


class TestPositionPublish:
    async def test_a_fix_is_published_on_its_feeds_channel(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        redis = FakeRedis()
        client = await make_public_client(redis)

        response = await client.post(
            "/ingest/position",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "tracker_id": "gently-tender-oyster",
                "trip_id": "T1",
                "lat": 42.0,
                "lon": -71.0,
                "timestamp": 1_700_000_000,
            },
        )

        assert response.status_code == 200
        (channel, payload) = redis.published[0]
        assert channel == feed_channel(world["feed"].id)
        event = json.loads(payload)
        assert event["type"] == "position"
        assert event["vehicle"]["key"] == "gently-tender-oyster"
        assert event["vehicle"]["trackerId"] == "gently-tender-oyster"
        assert event["vehicle"]["label"] == "Otter"

    async def test_the_published_vehicle_is_what_the_endpoint_would_return(
        self,
        make_public_client: ClientFactory,
        make_admin_client: ClientFactory,
        world: dict,
    ) -> None:
        redis = FakeRedis()
        ingest = await make_public_client(redis)
        await ingest.post(
            "/ingest/position",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "tracker_id": "gently-tender-oyster",
                "trip_id": "T1",
                "lat": 42.0,
                "lon": -71.0,
                "bearing": 90.0,
                "speed": 12.5,
                "route_id": "R1",
                "stop_id": "S1",
                "current_stop_sequence": 4,
                "current_status": "STOPPED_AT",
                "timestamp": 1_700_000_000,
            },
        )

        read = await make_admin_client(redis)
        response = await read.get(
            f"/api/feeds/{world['feed'].id}/tracker-positions", headers=OWNER
        )

        pushed = json.loads(redis.published[0][1])["vehicle"]
        assert response.json() == [pushed]
        # The enum travels as its number, which is what a decoded feed gives a
        # consumer and so what the map's own type holds.
        assert pushed["currentStatus"] == 1

    async def test_a_fix_for_an_unknown_tracker_is_stored_and_not_published(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        redis = FakeRedis()
        client = await make_public_client(redis)

        response = await client.post(
            "/ingest/position",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "tracker_id": "nobody-at-all",
                "trip_id": "T1",
                "lat": 42.0,
                "lon": -71.0,
                "timestamp": 1_700_000_000,
            },
        )

        assert response.status_code == 200
        assert "vehicle:nobody-at-all" in redis.data
        assert redis.published == []

    async def test_a_device_key_lands_in_the_surrogate_id_namespace(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        """A producer configured with the readable device_key still works.

        The serving side only ever reads the `vehicle:{Tracker.id}` namespace, so
        a device_key that was stored verbatim would be written where nothing
        reads it. This is the shape that broke hell-gate-bridge for five weeks.
        """
        redis = FakeRedis()
        client = await make_public_client(redis)

        response = await client.post(
            "/ingest/position",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "tracker_id": world["reporting"].device_key,
                "trip_id": "T1",
                "lat": 42.0,
                "lon": -71.0,
                "timestamp": 1_700_000_000,
            },
        )

        assert response.status_code == 200
        assert "vehicle:gently-tender-oyster" in redis.data
        assert f"vehicle:{world['reporting'].device_key}" not in redis.data
        # The stored record carries the surrogate id too, not what was sent.
        assert (
            json.loads(redis.data["vehicle:gently-tender-oyster"])["tracker_id"]
            == "gently-tender-oyster"
        )
        # Resolved, so it publishes like any known tracker.
        assert redis.published != []

    async def test_an_unknown_tracker_is_logged(
        self,
        make_public_client: ClientFactory,
        world: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Storing without publishing is deliberate, but must not be silent."""
        redis = FakeRedis()
        client = await make_public_client(redis)

        with caplog.at_level(logging.WARNING, logger="cafe_car.routers.ingest"):
            await client.post(
                "/ingest/position",
                headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
                json={
                    "tracker_id": "nobody-at-all",
                    "trip_id": "T1",
                    "lat": 42.0,
                    "lon": -71.0,
                    "timestamp": 1_700_000_000,
                },
            )

        assert "nobody-at-all" in caplog.text

    async def test_a_vehicle_that_changes_trip_keeps_one_record(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        """The regression the per-vehicle re-key exists for.

        A bus finishing loop 1 and starting loop 2 used to write a key under the
        new trip while the old one lived out its 60s TTL, so the feed carried two
        entities sharing one `vehicle.id`. Keyed on the vehicle, the second fix
        lands on the first one's key.
        """
        redis = FakeRedis()
        client = await make_public_client(redis)

        for trip_id in ("loop-1", "loop-2"):
            await client.post(
                "/ingest/position",
                headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
                json={
                    "tracker_id": "gently-tender-oyster",
                    "vehicle_id": "bus-42",
                    "trip_id": trip_id,
                    "lat": 42.0,
                    "lon": -71.0,
                    "timestamp": 1_700_000_000,
                },
            )

        assert list(redis.data) == ["vehicle:gently-tender-oyster:bus-42"]
        stored = json.loads(redis.data["vehicle:gently-tender-oyster:bus-42"])
        assert stored["trip_id"] == "loop-2"

    async def test_a_fleet_sharing_a_bare_key_is_logged(
        self,
        make_public_client: ClientFactory,
        world: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An absent vehicle_id is fine for one device and wrong for a fleet.

        A single Traccar device legitimately holds the bare tracker key. Two
        vehicles reporting different trips into it are overwriting each other,
        which is only visible here, at the write.
        """
        redis = FakeRedis()
        client = await make_public_client(redis)

        with caplog.at_level(logging.WARNING, logger="cafe_car.routers.ingest"):
            for trip_id in ("T1", "T2"):
                await client.post(
                    "/ingest/position",
                    headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
                    json={
                        "tracker_id": "gently-tender-oyster",
                        "trip_id": trip_id,
                        "lat": 42.0,
                        "lon": -71.0,
                        "timestamp": 1_700_000_000,
                    },
                )

        assert list(redis.data) == ["vehicle:gently-tender-oyster"]
        assert "vehicle_id" in caplog.text

    async def test_one_device_repeating_its_trip_is_not_logged(
        self,
        make_public_client: ClientFactory,
        world: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The ordinary Traccar case: same vehicle, same trip, fix after fix."""
        redis = FakeRedis()
        client = await make_public_client(redis)

        with caplog.at_level(logging.WARNING, logger="cafe_car.routers.ingest"):
            for _ in range(2):
                await client.post(
                    "/ingest/position",
                    headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
                    json={
                        "tracker_id": "gently-tender-oyster",
                        "trip_id": "T1",
                        "lat": 42.0,
                        "lon": -71.0,
                        "timestamp": 1_700_000_000,
                    },
                )

        assert caplog.text == ""


class TestTripUpdateIngest:
    """``/ingest/trip-update``, whose keyspace is scoped by tracker.

    A ``trip_id`` is only unique inside one feed's GTFS, so two feeds both
    numbering a trip ``"1"`` used to overwrite each other's predictions.
    """

    @staticmethod
    async def _post(client: AsyncClient, tracker_id: str, trip_id: str) -> None:
        response = await client.post(
            "/ingest/trip-update",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "tracker_id": tracker_id,
                "trip_id": trip_id,
                "timestamp": 1_700_000_000,
                "start_date": "20260803",
                "stop_time_updates": [
                    {"stop_sequence": 1, "arrival_delay": 60},
                ],
            },
        )
        assert response.status_code == 200

    async def test_the_key_is_scoped_by_the_tracker(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        redis = FakeRedis()
        client = await make_public_client(redis)

        await self._post(client, "gently-tender-oyster", "T1")

        assert list(redis.data) == ["trip_update:gently-tender-oyster:T1:20260803"]

    async def test_two_trackers_sharing_a_trip_id_do_not_collide(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        """The bug this scoping exists for: one feed's "1" is not another's."""
        redis = FakeRedis()
        client = await make_public_client(redis)

        await self._post(client, "gently-tender-oyster", "1")
        await self._post(client, "quietly-sleepy-heron", "1")

        assert sorted(redis.data) == [
            "trip_update:gently-tender-oyster:1:20260803",
            "trip_update:quietly-sleepy-heron:1:20260803",
        ]

    async def test_a_device_key_lands_in_the_surrogate_id_namespace(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        """Same normalisation the position path does, so a reader finds it."""
        redis = FakeRedis()
        client = await make_public_client(redis)

        await self._post(client, world["reporting"].device_key, "T1")

        assert list(redis.data) == ["trip_update:gently-tender-oyster:T1:20260803"]
        stored = json.loads(redis.data["trip_update:gently-tender-oyster:T1:20260803"])
        assert stored["tracker_id"] == "gently-tender-oyster"


class TestBatchIngest:
    """``/ingest/positions`` and ``/ingest/trip-updates``.

    One request per poll cycle instead of one per vehicle. The records are the
    singular routes' records, so what is asserted here is only what batching
    adds: every record lands, each one publishes, and a bad record takes its
    batch down rather than being silently dropped.
    """

    @staticmethod
    def _position(tracker_id: str, vehicle_id: str, trip_id: str) -> dict:
        return {
            "tracker_id": tracker_id,
            "vehicle_id": vehicle_id,
            "trip_id": trip_id,
            "lat": 42.0,
            "lon": -71.0,
            "timestamp": 1_700_000_000,
        }

    async def test_every_position_in_a_batch_is_stored_and_published(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        redis = FakeRedis()
        client = await make_public_client(redis)

        response = await client.post(
            "/ingest/positions",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "positions": [
                    self._position("gently-tender-oyster", "449", "T1"),
                    self._position("gently-tender-oyster", "450", "T2"),
                    self._position("quietly-sleepy-heron", "bus-7", "T3"),
                ]
            },
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "count": 3}
        assert sorted(redis.data) == [
            "vehicle:gently-tender-oyster:449",
            "vehicle:gently-tender-oyster:450",
            "vehicle:quietly-sleepy-heron:bus-7",
        ]
        assert len(redis.published) == 3

    async def test_a_bad_record_rejects_its_batch_and_writes_nothing(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        """Validation is whole-body, so a chunk fails as a unit.

        The alternative, dropping the offending record and answering 200, hides
        a broken producer behind a healthy-looking response.
        """
        redis = FakeRedis()
        client = await make_public_client(redis)
        bad = self._position("gently-tender-oyster", "450", "T2")
        del bad["lat"]

        response = await client.post(
            "/ingest/positions",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "positions": [
                    self._position("gently-tender-oyster", "449", "T1"),
                    bad,
                ]
            },
        )

        assert response.status_code == 422
        assert redis.data == {}

    async def test_a_batch_needs_the_ingest_token(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        redis = FakeRedis()
        client = await make_public_client(redis)

        response = await client.post(
            "/ingest/positions",
            json={"positions": [self._position("gently-tender-oyster", "449", "T1")]},
        )

        assert response.status_code == 403
        assert redis.data == {}

    async def test_every_trip_update_in_a_batch_is_stored(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        redis = FakeRedis()
        client = await make_public_client(redis)

        response = await client.post(
            "/ingest/trip-updates",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "trip_updates": [
                    {
                        "tracker_id": "gently-tender-oyster",
                        "trip_id": trip_id,
                        "timestamp": 1_700_000_000,
                        "stop_time_updates": [
                            {"stop_sequence": 1, "arrival_delay": 60}
                        ],
                    }
                    for trip_id in ("T1", "T2")
                ]
            },
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "count": 2}
        assert sorted(redis.data) == [
            "trip_update:gently-tender-oyster:T1",
            "trip_update:gently-tender-oyster:T2",
        ]

    async def test_a_device_key_is_normalised_once_for_the_whole_batch(
        self, make_public_client: ClientFactory, world: dict
    ) -> None:
        """The per-batch tracker cache must not change what gets written."""
        redis = FakeRedis()
        client = await make_public_client(redis)
        device_key = world["reporting"].device_key

        response = await client.post(
            "/ingest/positions",
            headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
            json={
                "positions": [
                    self._position(device_key, "449", "T1"),
                    self._position(device_key, "450", "T2"),
                ]
            },
        )

        assert response.status_code == 200
        assert sorted(redis.data) == [
            "vehicle:gently-tender-oyster:449",
            "vehicle:gently-tender-oyster:450",
        ]
