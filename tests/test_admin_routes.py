"""The admin app end to end, over ASGI.

These are the cases that cannot be reached by calling a helper: they depend on
the proxy headers, the session cookie, and SQLAdmin's own form handling. The
app is pointed at the same in-memory SQLite database the rest of the suite
uses; the Redis lifespan never runs, because ASGITransport does not start one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from railroad_club.models.feed import Feed
from sqlmodel.ext.asyncio.session import AsyncSession as SqlModelAsyncSession
from starlette.requests import Request

from tests.factories import PROVIDER, add_member, make_feed, make_user

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlmodel.ext.asyncio.session import AsyncSession


@pytest.fixture
async def client(
    engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncClient]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused:unused@db/unused")
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/1")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    monkeypatch.setenv("OIDC_PROVIDER", PROVIDER)

    import cafe_car.database as database
    from cafe_car.settings import get_settings

    get_settings.cache_clear()
    # The URL above is never dialled: every session factory goes through this
    # engine, which is the SQLite one the `session` fixture already populated.
    monkeypatch.setattr(database, "_engine", engine)

    from cafe_car.admin_main import create_admin_app

    app = create_admin_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    get_settings.cache_clear()


def _headers(subject: str, email: str) -> dict[str, str]:
    return {"X-Auth-Request-User": subject, "X-Auth-Request-Email": email}


async def _reread_feed(engine: AsyncEngine, feed_id: int) -> Feed | None:
    """Read a feed through a session of its own.

    The request handled its writes in a different session; re-reading through
    the test's session would answer from its identity map rather than from the
    database.
    """
    async with SqlModelAsyncSession(engine) as fresh:
        return await fresh.get(Feed, feed_id)


async def test_a_member_cannot_take_ownership_with_a_crafted_post(
    client: AsyncClient, session: AsyncSession, engine: AsyncEngine
) -> None:
    owner = await make_user(session, email="owner@example.com", subject="kc-owner")
    member = await make_user(session, email="member@example.com", subject="kc-member")
    feed = await make_feed(session, owner, "crafted-feed")
    await add_member(session, feed, member, added_by=owner)

    response = await client.post(
        f"/feed/edit/{feed.id}",
        headers=_headers("kc-member", "member@example.com"),
        data={
            "feed_name": "crafted-feed",
            "static_feed_url": "https://example.com/gtfs.zip",
            "owner_id": str(member.id),
        },
        follow_redirects=False,
    )

    assert response.status_code in (200, 302, 303)
    refreshed = await _reread_feed(engine, feed.id)
    assert refreshed.owner_id == owner.id, "owner_id must not be settable from a form"


async def test_update_model_drops_owner_id_even_when_it_reaches_the_data(
    client: AsyncClient, session: AsyncSession, engine: AsyncEngine
) -> None:
    """The route above is guarded twice over: `owner_id` is in
    `form_excluded_columns`, so WTForms never builds the field, *and*
    `update_model` pops the key. The first guard alone makes the HTTP test pass,
    which would leave the pop untested — so drive it directly.

    Takes the `client` fixture only for its side effect: `create_admin_app()`
    is what binds the engine and the async session maker onto the view class.
    """
    from cafe_car.admin.views import FeedAdmin

    owner = await make_user(session, email="owner@example.com")
    member = await make_user(session, email="member@example.com")
    feed = await make_feed(session, owner, "direct-feed")
    await add_member(session, feed, member, added_by=owner)

    view = FeedAdmin()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/feed/edit/{feed.id}",
            "headers": [],
            "path_params": {"pk": str(feed.id)},
            "session": {"user_id": member.id},
        }
    )
    async with SqlModelAsyncSession(engine, expire_on_commit=False) as db:
        request.state.session = db
        await view.update_model(
            request,
            feed.id,
            {
                "feed_name": "direct-feed",
                "static_feed_url": "https://example.com/gtfs.zip",
                "owner_id": member.id,
            },
        )

    assert (await _reread_feed(engine, feed.id)).owner_id == owner.id


async def test_a_member_cannot_delete_a_shared_feed(
    client: AsyncClient, session: AsyncSession, engine: AsyncEngine
) -> None:
    owner = await make_user(session, email="owner@example.com", subject="kc-owner")
    member = await make_user(session, email="member@example.com", subject="kc-member")
    feed = await make_feed(session, owner, "undeletable")
    await add_member(session, feed, member, added_by=owner)

    response = await client.request(
        "DELETE",
        f"/feed/delete?pks={feed.id}",
        headers=_headers("kc-member", "member@example.com"),
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert await _reread_feed(engine, feed.id) is not None


async def test_a_stranger_cannot_reach_someone_elses_feed(
    client: AsyncClient, session: AsyncSession
) -> None:
    owner = await make_user(session, email="owner@example.com", subject="kc-owner")
    await make_user(session, email="stranger@example.com", subject="kc-stranger")
    feed = await make_feed(session, owner, "private-feed")

    response = await client.get(
        f"/feed/details/{feed.id}",
        headers=_headers("kc-stranger", "stranger@example.com"),
    )

    assert response.status_code == 404


async def test_no_proxy_header_is_not_authenticated(client: AsyncClient) -> None:
    response = await client.get("/feed/list", follow_redirects=False)
    assert response.status_code in (302, 303, 403)


async def test_a_stale_session_without_a_user_id_re_authenticates(
    client: AsyncClient, session: AsyncSession, engine: AsyncEngine
) -> None:
    """A session cookie minted before the identity split carries only
    `subject`. It must not be trusted, and must not lock the user out — the
    proxy header is re-resolved on every request."""
    user = await make_user(session, email="stale@example.com", subject="kc-stale")
    feed = await make_feed(session, user, "stale-feed")

    first = await client.get(
        "/feed/list", headers=_headers("kc-stale", "stale@example.com")
    )
    assert first.status_code == 200
    assert "stale-feed" in first.text

    # Drop the cookie entirely — the harshest version of a session that no
    # longer carries what the app needs.
    client.cookies.clear()
    second = await client.get(
        "/feed/list", headers=_headers("kc-stale", "stale@example.com")
    )
    assert second.status_code == 200
    assert "stale-feed" in second.text
    assert (await _reread_feed(engine, feed.id)).owner_id == user.id


async def test_a_cookie_from_another_user_does_not_override_the_header(
    client: AsyncClient, session: AsyncSession
) -> None:
    """entity_router sits outside SQLAdmin, so nothing runs `authenticate` for
    it. The proxy header has to be the authority, not the cookie."""
    alice = await make_user(session, email="alice@example.com", subject="kc-alice")
    await make_user(session, email="bob@example.com", subject="kc-bob")
    feed = await make_feed(session, alice, "alice-only")

    # Sign in as alice so the cookie holds her user_id...
    await client.get("/feed/list", headers=_headers("kc-alice", "alice@example.com"))
    # ...then present it alongside bob's header.
    response = await client.get(
        f"/feeds/{feed.id}/members-partial",
        headers=_headers("kc-bob", "bob@example.com"),
    )

    assert response.status_code == 403
