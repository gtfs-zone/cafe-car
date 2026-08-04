"""Where a feed's public GTFS-RT endpoints live.

The path shapes are declared by ``routers/gtfs_rt.py``; this is the one place
that spells them out as URLs. Both consumers (the admin UI's viz deep link
in ``admin/links.py`` and the public feed catalog in ``routers/catalog.py``)
come through here, so a renamed route breaks in one place rather than three.

``PUBLIC_RT_BASE`` is hardcoded to prod for the same reason the frontend bases
are (see ``admin/links.py``): a feed created locally will not exist in prod, and
that is an accepted trade-off against keeping deploy-specific config out of the
response body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from railroad_club.models.feed import Feed

PUBLIC_RT_BASE = "https://rt.gtfs.zone"


class RealtimeUrls(NamedTuple):
    vehicle_positions: str
    trip_updates: str
    service_alerts: str


def feed_rt_urls(feed: Feed) -> RealtimeUrls:
    """The three public GTFS-RT endpoint URLs for a feed."""
    base = f"{PUBLIC_RT_BASE}/{feed.feed_name}"
    return RealtimeUrls(
        vehicle_positions=f"{base}/vehicle_positions.pb",
        trip_updates=f"{base}/trip_updates.pb",
        service_alerts=f"{base}/service_alerts.pb",
    )
