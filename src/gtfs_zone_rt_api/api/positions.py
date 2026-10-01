"""``GET /api/feeds/{id}/tracker-positions``: where this feed's fleet is now.

The bootstrap half of the live map. The event channel pushes each fix as it
lands, but a client that has just selected a feed has missed every fix already
in flight, and a tracker reporting once a minute would leave the map empty for
most of that minute. This answers "everything live right now" in one request,
in exactly the shape the pushed events use, so the client stores both the same
way.

Presence is freshness. A ``vehicle:*`` record carries a 60s TTL, so a key that
is still there is a fix from within the last minute and a tracker that is not
in this response has not reported recently. There is no "last seen an hour ago"
here, because Redis no longer holds one.

Authenticated and feed-scoped, unlike the public ``.pb``. That is what lets it
key by the surrogate ``Tracker.id`` rather than by nickname: the published feed
deliberately *labels* vehicles by nickname, which is a display concern, and this
one needs an identity the map can click on.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from gtfs_zone_db_models.models.tracker import Tracker
from sqlmodel import select

from gtfs_zone_rt_api.api.deps import AccessibleFeed, DBSession
from gtfs_zone_rt_api.vehicle_payload import feed_vehicles

router = APIRouter()


@router.get("/feeds/{feed_id}/tracker-positions")
async def tracker_positions(
    feed: AccessibleFeed, session: DBSession, request: Request
) -> list[dict[str, Any]]:
    """Every live vehicle belonging to this feed's trackers.

    Untyped on purpose. The payload is the same open GTFS-RT-shaped vehicle the
    ``position`` event carries, built by one function in ``vehicle_payload.py``,
    and a response model here would be a second declaration of that shape to
    keep in step with it -- including ``raw``, which is whatever the producer
    wrote and has no schema at all.
    """
    rows = await session.execute(
        select(Tracker.id, Tracker.nickname).where(Tracker.feed_id == feed.id)
    )
    return await feed_vehicles(request.app.state.redis, rows.all())
