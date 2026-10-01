"""Deep links from the admin UI to the sibling gtfs.zone frontends.

Prod hostnames are hardcoded on purpose: the viz (rt-viewer) and editor
(gtfs-zone-editor) URLs change often in local/dev, so linking to prod keeps this
simple and stable. A feed created locally will not exist in prod viz/editor;
that is an accepted trade-off.

URL schemes:
- viz (rt-viewer):
    https://viz.rt.gtfs.zone/#static=<url>&rt_vp=<url>&rt_tu=<url>&rt_al=<url>&cors=s,r
- editor (gtfs-zone-editor):
    https://edit.gtfs.zone/#load=<static_url>
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from gtfs_zone_rt_api.feed_urls import feed_rt_urls, feed_static_url

if TYPE_CHECKING:
    from gtfs_zone_db_models.models.feed import Feed

VIZ_BASE = "https://viz.rt.gtfs.zone"
EDITOR_BASE = "https://edit.gtfs.zone"


def viz_url(feed: Feed) -> str:
    """Build a viz.rt.gtfs.zone deep link that loads this feed's static +
    realtime sources. Routes both through the CORS proxy (``cors=s,r``)."""
    rt = feed_rt_urls(feed)
    params = {
        "static": feed_static_url(feed) or "",
        "rt_vp": rt.vehicle_positions,
        "rt_tu": rt.trip_updates,
        "rt_al": rt.service_alerts,
        "cors": "s,r",
    }
    return f"{VIZ_BASE}/#{urlencode(params)}"


def editor_url(feed: Feed) -> str | None:
    """Build an edit.gtfs.zone link that opens this feed's static GTFS in the
    editor. Returns ``None`` when the feed has no static URL to load."""
    url = feed_static_url(feed)
    if not url:
        return None
    return f"{EDITOR_BASE}/#load={quote(url, safe='')}"
