"""Deep links from the admin UI to the sibling gtfs.zone frontends.

Prod hostnames are hardcoded on purpose: the viz (test-track) and editor
(coloring-book) URLs change often in local/dev, so linking to prod keeps this
simple and stable. A feed created locally will not exist in prod viz/editor —
that is an accepted trade-off.

URL schemes:
- viz (test-track):
    https://viz.rt.gtfs.zone/#static=<url>&rt_vp=<url>&rt_tu=<url>&rt_al=<url>&cors=s,r
- editor (coloring-book):
    https://edit.gtfs.zone/#load=<static_url>
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from cafe_car.feed_urls import feed_rt_urls

if TYPE_CHECKING:
    from railroad_club.models.feed import Feed

VIZ_BASE = "https://viz.rt.gtfs.zone"
EDITOR_BASE = "https://edit.gtfs.zone"


def viz_url(feed: Feed) -> str:
    """Build a viz.rt.gtfs.zone deep link that loads this feed's static +
    realtime sources. Routes both through the CORS proxy (``cors=s,r``)."""
    rt = feed_rt_urls(feed)
    params = {
        "static": feed.static_feed_url or "",
        "rt_vp": rt.vehicle_positions,
        "rt_tu": rt.trip_updates,
        "rt_al": rt.service_alerts,
        "cors": "s,r",
    }
    return f"{VIZ_BASE}/#{urlencode(params)}"


def editor_url(feed: Feed) -> str | None:
    """Build an edit.gtfs.zone link that opens this feed's static GTFS in the
    editor. Returns ``None`` when the feed has no static URL to load."""
    if not feed.static_feed_url:
        return None
    return f"{EDITOR_BASE}/#load={quote(feed.static_feed_url, safe='')}"
