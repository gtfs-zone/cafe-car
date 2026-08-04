"""Which service alerts a feed is currently publishing.

Shared by the public ``service_alerts.pb`` serializer and the feed catalog, so
the two can never disagree: a catalog that reports ``has_alerts`` for a feed
whose ``.pb`` comes back empty would be worse than no flag at all.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from railroad_club.models.service_alert import ServiceAlert


def to_utc(dt: datetime) -> datetime:
    """Anchor a naive timestamp to UTC.

    SQLite hands back naive datetimes where Postgres hands back aware ones, and
    comparing the two raises. Everything stored is UTC, so saying so is safe.
    """
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def active_alerts(
    alerts: Iterable[ServiceAlert], now: datetime
) -> list[ServiceAlert]:
    """Alerts inside their active period that name at least one informed entity.

    An alert with no informed entities selects nothing, so it is dropped rather
    than published as an entity no consumer can match.

    ``.entities`` is touched here, so callers must have eager-loaded it
    (``selectinload(ServiceAlert.entities)``), or the session must still be open.
    """
    return [
        a
        for a in alerts
        if (a.active_period_start is None or to_utc(a.active_period_start) <= now)
        and (a.active_period_end is None or to_utc(a.active_period_end) > now)
        and a.entities
    ]
