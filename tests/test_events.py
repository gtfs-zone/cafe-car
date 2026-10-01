"""The SSE channel, over ASGI.

Two properties matter here and nothing else can assert them.

* The channel is **scoped like every other feed route**. A stranger opening a
  stream would be a live push of somebody else's feed, which is worse than a
  leaked read: it keeps leaking.
* The first frame is the **current** load status. A client that had to poll
  once to bootstrap would show "never loaded" for as long as the feed stayed
  quiet, which for a feed loaded daily is most of the time.

The app is driven as a raw ASGI callable rather than through ``AsyncClient``.
``ASGITransport`` awaits the application to completion before it builds a
response, so it can read a finite body and nothing else; a stream that never
ends would simply hang it. :func:`collect` runs the app as a task instead,
gathers body chunks as they are sent, and cancels once it has the frames it
asked for, which is also what exercises the disconnect path.

Nothing runs the lifespan either, so ``app.state.redis`` is set by hand; the
fake below implements only the pub/sub calls the router makes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest
from gtfs_zone_db_models.feed_events import feed_channel
from gtfs_zone_db_models.models.gtfs_static import GtfsStaticFeed, LoadStatus

from tests.factories import PROVIDER, add_member, make_feed, make_user

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlmodel.ext.asyncio.session import AsyncSession

    type AppFactory = Callable[[FakePubSub], FastAPI]


class FakePubSub:
    """A queue with the four methods ``api/events.py`` calls.

    ``get_message`` answers bytes, because the real client is opened without
    ``decode_responses``; a stringly-typed fake would hide exactly the bug the
    forwarder's ``.decode()`` exists to avoid. Draining the queue makes it
    behave like an idle channel: wait the timeout out, then answer ``None``.
    """

    def __init__(self, messages: list[bytes] | None = None) -> None:
        self.queue = list(messages or [])
        self.channels: list[str] = []
        self.unsubscribed = False
        self.closed = False

    def pubsub(self) -> FakePubSub:
        return self

    async def subscribe(self, channel: str) -> None:
        self.channels.append(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float = 0.0
    ) -> dict[str, Any] | None:
        if self.queue:
            return {"type": "message", "data": self.queue.pop(0)}
        await asyncio.sleep(timeout)
        return None

    async def unsubscribe(self) -> None:
        self.unsubscribed = True

    async def aclose(self) -> None:
        self.closed = True


class Result:
    """What one stream said before it was cut off."""

    def __init__(self, status: int, headers: dict[str, str], frames: list[str]) -> None:
        self.status = status
        self.headers = headers
        self.frames = frames

    def payload(self, index: int) -> dict:
        frame = self.frames[index]
        assert frame.startswith("data: "), frame
        return json.loads(frame.removeprefix("data: "))


async def collect(
    app: FastAPI, path: str, headers: dict[str, str], frames: int = 1
) -> Result:
    """Open ``path``, read ``frames`` SSE frames, then hang up.

    A frame is anything up to a blank line, so a heartbeat comment counts as
    one; a test that wants an event after a heartbeat asks for both.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [
            (b"host", b"test"),
            *((k.lower().encode(), v.encode()) for k, v in headers.items()),
        ],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }

    status = 0
    response_headers: dict[str, str] = {}
    buffer = b""
    collected: list[str] = []
    enough = asyncio.Event()
    disconnected = asyncio.Event()

    async def receive() -> dict[str, Any]:
        # The client never sends anything and never goes away on its own; the
        # hang-up is the task being cancelled below.
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, response_headers, buffer
        if message["type"] == "http.response.start":
            status = message["status"]
            response_headers = {
                k.decode().lower(): v.decode() for k, v in message["headers"]
            }
        elif message["type"] == "http.response.body":
            buffer += message.get("body", b"")
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                collected.append(frame.decode())
            if len(collected) >= frames or not message.get("more_body", False):
                enough.set()

    task = asyncio.create_task(app(scope, receive, send))
    waiter = asyncio.create_task(enough.wait())
    done, _ = await asyncio.wait(
        {task, waiter}, timeout=5, return_when=asyncio.FIRST_COMPLETED
    )
    waiter.cancel()
    assert done, "the endpoint produced nothing within the timeout"

    # Hang up. A finite response (a 404) has already returned; an open stream
    # is cancelled, which is what the real disconnect path does.
    disconnected.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    return Result(status, response_headers, collected)


@pytest.fixture
def make_app(
    engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AppFactory:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@db/unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("OIDC_PROVIDER", PROVIDER)

    import gtfs_zone_rt_api.database as database
    from gtfs_zone_rt_api.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(database, "_engine", engine)

    from gtfs_zone_rt_api.admin_main import create_admin_app

    def build(redis: FakePubSub) -> FastAPI:
        app = create_admin_app()
        app.state.redis = redis
        return app

    return build


def _headers(subject: str, email: str) -> dict[str, str]:
    return {"X-Auth-Request-User": subject, "X-Auth-Request-Email": email}


OWNER = _headers("kc-owner", "owner@example.com")
MEMBER = _headers("kc-member", "member@example.com")
STRANGER = _headers("kc-stranger", "stranger@example.com")


@pytest.fixture
async def world(session: AsyncSession) -> AsyncGenerator[dict]:
    owner = await make_user(session, email="owner@example.com", subject="kc-owner")
    member = await make_user(session, email="member@example.com", subject="kc-member")
    await make_user(session, email="stranger@example.com", subject="kc-stranger")

    static = GtfsStaticFeed(status=LoadStatus.success, timezone="America/New_York")
    session.add(static)
    await session.commit()

    feed = await make_feed(session, owner, "owner-feed")
    feed.gtfs_static_feed_id = static.id
    session.add(feed)
    await add_member(session, feed, member, added_by=owner)

    fresh = await make_feed(session, owner, "fresh-feed")

    yield {"feed": feed, "fresh": fresh}


async def test_stranger_cannot_subscribe(make_app: AppFactory, world: dict) -> None:
    redis = FakePubSub()
    app = make_app(redis)
    result = await collect(app, f"/api/feeds/{world['feed'].id}/events", STRANGER)

    assert result.status == 404
    # A refused caller costs no subscription either.
    assert redis.channels == []


async def test_first_frame_is_the_current_load_status(
    make_app: AppFactory, world: dict
) -> None:
    app = make_app(FakePubSub())
    result = await collect(app, f"/api/feeds/{world['feed'].id}/events", OWNER)

    assert result.status == 200
    assert result.headers["content-type"].startswith("text/event-stream")
    assert result.headers["x-accel-buffering"] == "no"

    first = result.payload(0)
    assert first["type"] == "load"
    assert first["load"]["status"] == "success"
    assert first["load"]["timezone"] == "America/New_York"


async def test_a_feed_never_loaded_reports_null_rather_than_pending(
    make_app: AppFactory, world: dict
) -> None:
    """A feed with no ``gtfs_static_feed`` row has not been queued, it has been
    created; saying "pending" would claim it is already on its way."""
    app = make_app(FakePubSub())
    result = await collect(app, f"/api/feeds/{world['fresh'].id}/events", OWNER)

    assert result.payload(0) == {"type": "load", "load": None}


async def test_a_member_gets_the_feeds_own_channel(
    make_app: AppFactory, world: dict
) -> None:
    redis = FakePubSub()
    app = make_app(redis)
    await collect(app, f"/api/feeds/{world['feed'].id}/events", MEMBER)

    assert redis.channels == [feed_channel(world["feed"].id)]


async def test_published_payloads_are_forwarded_unparsed(
    make_app: AppFactory, world: dict
) -> None:
    """The forwarder does not know what an event means, which is what lets a
    new event type ship without touching this app."""
    published = b'{"type":"something-new","whatever":[1,2,3]}'
    app = make_app(FakePubSub([published]))
    result = await collect(
        app, f"/api/feeds/{world['feed'].id}/events", OWNER, frames=2
    )

    assert result.frames[1] == f"data: {published.decode()}"


async def test_an_idle_channel_heartbeats(
    make_app: AppFactory, world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gtfs_zone_rt_api.api import events

    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.01)
    app = make_app(FakePubSub())
    result = await collect(
        app, f"/api/feeds/{world['feed'].id}/events", OWNER, frames=2
    )

    # A comment, not an event: a client's `onmessage` must never see it.
    assert result.frames[1].startswith(":")


async def test_hanging_up_releases_the_subscription(
    make_app: AppFactory, world: dict
) -> None:
    """A long session opens one of these per feed switch, so a connection that
    is not given back is a leak that grows all day."""
    redis = FakePubSub()
    app = make_app(redis)
    await collect(app, f"/api/feeds/{world['feed'].id}/events", OWNER)
    # The cancellation unwinds through the generator's `finally`, which may land
    # a tick after the task is cancelled.
    for _ in range(50):
        if redis.closed:
            break
        await asyncio.sleep(0.01)

    assert redis.unsubscribed
    assert redis.closed
