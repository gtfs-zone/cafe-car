"""``GET /api/feeds/{id}/events``: one feed's live channel, as SSE.

Three things make this endpoint what it is.

**Current state first.** The first frame is the feed's load status as it stands
right now, so a client never has to poll once to bootstrap and never has to
reconcile a push against a separate fetch. Everything after it is whatever
static-importer publishes.

**A dumb forwarder.** What comes off Redis is written to the wire without being
parsed. The payloads are gtfs-zone-db-models's and every one of them carries a
``type``, so a new event type is a publisher change and a client change with no
change here in between. It also means a malformed publish cannot take the
stream down.

**Nothing touches the database while streaming.** ``DBSessionMiddleware`` wraps
the request in a session that is closed as soon as the route returns its
response, which for a streaming response is *before* the first byte of the body
is produced. So the load status is read up front, in the route, and the
generator afterwards only ever talks to Redis. A query inside it would run
against a closed session.

The heartbeat is a comment frame rather than an event, so a client's
``onmessage`` never sees it. It is there for the proxies: oauth2-proxy and
Traefik both drop a connection that has been silent long enough, and a feed
that loads once a day is silent for a very long time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from gtfs_zone_db_models.feed_events import feed_channel, load_event
from gtfs_zone_db_models.models.gtfs_static import GtfsStaticFeed

from gtfs_zone_rt_api.api.deps import AccessibleFeed, DBSession

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis

# Long enough to be cheap on an idle feed, short enough to stay under the
# idle timeouts in front of this app.
HEARTBEAT_SECONDS = 20

router = APIRouter()


def _frame(payload: str) -> str:
    """One SSE ``data:`` frame. The payload is JSON, so it has no newline."""
    return f"data: {payload}\n\n"


async def _stream(
    redis: Redis, feed_id: int, first: dict[str, Any], request: Request
) -> AsyncIterator[str]:
    yield _frame(json.dumps(first))

    pubsub = redis.pubsub()
    await pubsub.subscribe(feed_channel(feed_id))
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
            )
            if message is None:
                # No traffic for a heartbeat's worth of time. The comment is
                # also how a dead socket is discovered: the write is what
                # fails, and an idle feed would otherwise never write.
                if await request.is_disconnected():
                    return
                yield ": keep-alive\n\n"
                continue
            data = message["data"]
            yield _frame(data.decode() if isinstance(data, bytes) else str(data))
    finally:
        # Unsubscribe as well as close: the connection goes back to a pool that
        # a later subscriber will take it from.
        await pubsub.unsubscribe()
        await pubsub.aclose()


@router.get("/feeds/{feed_id}/events")
async def feed_events(
    feed: AccessibleFeed, session: DBSession, request: Request
) -> StreamingResponse:
    """Subscribe to a feed's channel, current load status first.

    Scoped like every other feed route: a feed the caller cannot see is a 404
    from ``accessible_feed`` before a connection is opened.
    """
    static = (
        await session.get(GtfsStaticFeed, feed.gtfs_static_feed_id)
        if feed.gtfs_static_feed_id
        else None
    )

    return StreamingResponse(
        _stream(request.app.state.redis, feed.id, load_event(static), request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx buffers a proxied response by default, which would hold
            # every event back until the stream ended. Ignored by proxies that
            # do not know it.
            "X-Accel-Buffering": "no",
        },
    )
