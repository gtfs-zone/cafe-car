"""The public feed catalog.

``GET /feeds`` is what test-track's Load modal is populated from: every feed
this server publishes, the four URLs needed to visualize it, and whether each
realtime endpoint currently has anything in it.

The liveness flags are computed the way the ``.pb`` serializers compute their
payloads; a flag that disagrees with the feed it describes would be worse than
no flag. Vehicles come from the ``vehicle:*`` keyspace, whose 60s TTL makes
presence the same thing as freshness; trip updates are only published for a trip
some live vehicle is running (``routers/gtfs_rt.py``), so they are looked up
through those vehicles rather than by scanning ``trip_update:*``; alerts go
through the same filter the serializer uses (``cafe_car.alerts``).

Deliberately public and unauthenticated, unlike ``internal.py::feed_urls``: it
carries only what an unauthenticated ``.pb`` request would already reveal. The
tracker credential (``Tracker.device_key``) must never appear in the response.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from railroad_club.models.feed import Feed
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker
from sqlalchemy.orm import selectinload
from sqlmodel import select

from cafe_car.alerts import active_alerts
from cafe_car.database import get_session
from cafe_car.feed_urls import feed_rt_urls
from cafe_car.vehicle_payload import live_vehicle_keys

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()

# The flags turn over on the order of a producer's poll cycle, and computing
# them costs a Redis keyspace walk. Half a poll's worth of staleness is a fair
# trade for not doing that per page-load.
CACHE_CONTROL = "public, max-age=30"

class FeedCatalogEntry(BaseModel):
    """One feed, as a consumer needs to see it."""

    feed_name: str
    static_url: str
    vehicle_positions_url: str
    trip_updates_url: str
    service_alerts_url: str
    has_vehicles: bool
    has_trip_updates: bool
    has_alerts: bool


async def _any_trip_update(redis: Redis, vehicle_keys: list[str]) -> bool:
    """Whether any live vehicle here is running a trip that has an update.

    Mirrors the key derivation in ``gtfs_rt.py::trip_updates``: a >24h daily trip
    has several instances live at once, distinguished by ``start_date``.
    """
    if not vehicle_keys:
        return False
    for raw in await redis.mget(vehicle_keys):
        if raw is None:
            continue
        data = json.loads(raw)
        trip_id = data.get("trip_id")
        if not trip_id:
            continue
        start_date = data.get("start_date")
        tu_key = (
            f"trip_update:{trip_id}:{start_date}"
            if start_date
            else f"trip_update:{trip_id}"
        )
        if await redis.exists(tu_key):
            return True
    return False


@router.get("/feeds")
async def list_feeds(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[FeedCatalogEntry]:
    """Every feed on this server, with its URLs and what it is currently serving."""
    now = datetime.now(UTC)
    redis: Redis = request.app.state.redis

    feeds = (await session.exec(select(Feed))).all()

    trackers_by_feed: dict[int, list[str]] = defaultdict(list)
    for tracker_id, feed_id in await session.exec(
        select(Tracker.id, Tracker.feed_id)
    ):
        trackers_by_feed[feed_id].append(tracker_id)

    alerts_by_feed: dict[int, list[ServiceAlert]] = defaultdict(list)
    for alert in await session.exec(
        select(ServiceAlert).options(selectinload(ServiceAlert.entities))
    ):
        alerts_by_feed[alert.feed_id].append(alert)

    live = await live_vehicle_keys(redis)

    entries: list[FeedCatalogEntry] = []
    for feed in feeds:
        vehicle_keys = [
            key
            for tracker_id in trackers_by_feed[feed.id]
            for key in live.get(tracker_id, ())
        ]
        rt = feed_rt_urls(feed)
        entries.append(
            FeedCatalogEntry(
                feed_name=feed.feed_name,
                static_url=feed.static_feed_url or "",
                vehicle_positions_url=rt.vehicle_positions,
                trip_updates_url=rt.trip_updates,
                service_alerts_url=rt.service_alerts,
                has_vehicles=bool(vehicle_keys),
                has_trip_updates=await _any_trip_update(redis, vehicle_keys),
                has_alerts=bool(active_alerts(alerts_by_feed[feed.id], now)),
            )
        )

    response.headers["Cache-Control"] = CACHE_CONTROL
    return entries
