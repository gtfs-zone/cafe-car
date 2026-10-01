"""Where a feed's public GTFS-RT endpoints live.

The path shapes are declared by ``routers/gtfs_rt.py`` and
``routers/static_feed.py``; this is the one place that spells them out as
URLs. Every consumer (the public feed catalog in ``routers/catalog.py`` and the
API's ``FeedOut``) comes through here, so a renamed route breaks in one place
rather than three.

``PUBLIC_RT_BASE`` is hardcoded to prod: a feed created locally will not exist
in prod, and that is an accepted trade-off against keeping deploy-specific
config out of the response body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from gtfs_zone_db_models.models.feed import Feed

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


def feed_static_url(feed: Feed) -> str | None:
    """Where a consumer downloads this feed's schedule zip.

    A hosted feed's bytes live in the object store, and this is the only URL
    anybody outside the stack is given for them: permanent, unauthenticated,
    and unchanged by a storage swap. Nothing inside the stack fetches it -
    static-importer reads the object by key - so a load does not depend on the
    public app being up.

    A url-sourced feed answers with whatever URL it was pointed at, and a feed
    that is somehow neither answers None rather than a URL that 404s.
    """
    if feed.is_hosted:
        return f"{PUBLIC_RT_BASE}/{feed.feed_name}/gtfs.zip"
    return feed.static_feed_url
