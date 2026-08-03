"""The admin app end to end, over ASGI.

These are the cases that cannot be reached by calling a helper: they depend on
the proxy headers, the session cookie, and SQLAdmin's own form handling. The
app is pointed at the same in-memory SQLite database the rest of the suite
uses; the Redis lifespan never runs, because ASGITransport does not start one.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from railroad_club.models.feed import Feed
from railroad_club.models.identity import Identity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker
from railroad_club.models.tracker_rule import TrackerRule
from sqlalchemy import select
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

    # The edit page is the only page for a feed now, so it is where scoping has
    # to hold. Asserting on /feed/details would prove nothing: can_view_details
    # is False, so it 403s before the scoped query ever runs.
    response = await client.get(
        f"/feed/edit/{feed.id}",
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


async def test_the_feed_hub_renders_everything_beneath_the_feed(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The regression test for the trap CLAUDE.md says has bitten twice.

    SQLAdmin closes the query's session before rendering, so every relationship
    the hub template touches has to be eager loaded in `form_edit_query`. Drop
    one of those `selectinload`s and this is a DetachedInstanceError, not a
    subtly wrong page — which is exactly why it is asserted here.
    """
    owner = await make_user(session, email="hub@example.com", subject="kc-hub")
    feed = await make_feed(session, owner, "hub-feed")
    tracker = Tracker(id="gently-tender-oyster", nickname="Busbird", feed_id=feed.id)
    session.add(tracker)
    await session.flush()
    session.add(
        TrackerRule(
            tracker_id=tracker.id,
            trip_id="trip-42",
            monday=True,
            start_time=time(6, 0),
            end_time=time(22, 0),
        )
    )
    session.add(
        ServiceAlert(
            feed_id=feed.id,
            header_text="Bridge is out",
            description_text="Expect delays.",
        )
    )
    await session.commit()

    response = await client.get(
        f"/feed/edit/{feed.id}", headers=_headers("kc-hub", "hub@example.com")
    )

    assert response.status_code == 200
    assert "Busbird" in response.text
    assert "/tracker/edit/gently-tender-oyster" in response.text
    assert "Bridge is out" in response.text
    assert "/service-alert/edit/" in response.text
    # The People panel is htmx-loaded, so the hub only has to carry the trigger.
    assert f"/feeds/{feed.id}/members-partial" in response.text
    assert "/vendor/htmx.min.js" in response.text


async def test_the_feed_hub_does_not_leak_the_tracker_credential(
    client: AsyncClient, session: AsyncSession
) -> None:
    """`Tracker.id` is the Traccar credential. It belongs on the tracker's own
    page, not on the page you open to talk about who a feed is shared with."""
    owner = await make_user(session, email="secret@example.com", subject="kc-secret")
    feed = await make_feed(session, owner, "secret-feed")
    session.add(
        Tracker(id="wildly-mellow-heron", nickname="Riverliner", feed_id=feed.id)
    )
    await session.commit()

    response = await client.get(
        f"/feed/edit/{feed.id}", headers=_headers("kc-secret", "secret@example.com")
    )

    assert response.status_code == 200
    assert "Riverliner" in response.text
    # The href necessarily contains the id; no other occurrence should.
    assert response.text.count("wildly-mellow-heron") == 1
    assert 'href="/tracker/edit/wildly-mellow-heron"' in response.text


async def test_the_tracker_page_shows_the_credential_and_its_rules(
    client: AsyncClient, session: AsyncSession
) -> None:
    owner = await make_user(session, email="trk@example.com", subject="kc-trk")
    feed = await make_feed(session, owner, "tracker-feed")
    tracker = Tracker(id="quietly-brave-otter", nickname="Nightowl", feed_id=feed.id)
    session.add(tracker)
    await session.flush()
    session.add(
        TrackerRule(
            tracker_id=tracker.id,
            trip_id="owl-1",
            sunday=True,
            start_time=time(23, 0),
            end_time=time(3, 0),
        )
    )
    await session.commit()

    response = await client.get(
        f"/tracker/edit/{tracker.id}", headers=_headers("kc-trk", "trk@example.com")
    )

    assert response.status_code == 200
    assert "quietly-brave-otter" in response.text
    assert "owl-1" in response.text
    assert "/tracker-rule/edit/" in response.text


async def test_details_pages_are_gone(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Documents the merge: view and edit are one page, and it is the edit one."""
    owner = await make_user(session, email="det@example.com", subject="kc-det")
    feed = await make_feed(session, owner, "detail-feed")

    response = await client.get(
        f"/feed/details/{feed.id}", headers=_headers("kc-det", "det@example.com")
    )

    assert response.status_code == 403


async def test_the_feed_list_links_the_name_and_badges_access(
    client: AsyncClient, session: AsyncSession
) -> None:
    owner = await make_user(session, email="lst@example.com", subject="kc-lst")
    member = await make_user(session, email="mem@example.com", subject="kc-mem")
    feed = await make_feed(session, owner, "listed-feed")
    await add_member(session, feed, member, added_by=owner)

    as_owner = await client.get(
        "/feed/list", headers=_headers("kc-lst", "lst@example.com")
    )
    assert as_owner.status_code == 200
    assert f'href="/feed/edit/{feed.id}"' in as_owner.text
    assert '<span class="badge bg-green">owner</span>' in as_owner.text
    # No view/edit icons and no details route survive in the row actions.
    assert "/feed/details/" not in as_owner.text
    assert "fa-pen-to-square" not in as_owner.text
    assert "fa-eye" not in as_owner.text
    assert "fa-trash" in as_owner.text

    client.cookies.clear()
    as_member = await client.get(
        "/feed/list", headers=_headers("kc-mem", "mem@example.com")
    )
    assert as_member.status_code == 200
    # Grey, not blue: a blue badge here read as a broken link.
    assert '<span class="badge bg-secondary">shared with me</span>' in as_member.text


async def test_relation_cells_link_to_edit_not_details(
    client: AsyncClient, session: AsyncSession
) -> None:
    """list.html is forked precisely because it links relation cells to
    admin:details unconditionally, which now 403s."""
    owner = await make_user(session, email="rel@example.com", subject="kc-rel")
    feed = await make_feed(session, owner, "relation-feed")
    session.add(Tracker(id="boldly-sleepy-crane", nickname="Crane", feed_id=feed.id))
    await session.commit()

    response = await client.get(
        "/tracker/list", headers=_headers("kc-rel", "rel@example.com")
    )

    assert response.status_code == 200
    assert f"/feed/edit/{feed.id}" in response.text
    assert "/feed/details/" not in response.text


async def test_htmx_is_served_locally(client: AsyncClient) -> None:
    """Vendored, not CDN: an unpkg outage would leave every htmx panel stuck on
    'Loading…', which is the bug this replaced."""
    response = await client.get("/vendor/htmx.min.js")

    assert response.status_code == 200
    assert "htmx" in response.text[:200]


async def test_a_hostile_nickname_is_escaped_in_the_list(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Nicknames are free text and feeds are shared, so an unescaped formatter
    is stored XSS against everyone the feed is shared with."""
    owner = await make_user(session, email="xss@example.com", subject="kc-xss")
    feed = await make_feed(session, owner, "xss-feed")
    session.add(
        Tracker(
            id="sharply-clever-vole",
            nickname="<script>alert(1)</script>",
            feed_id=feed.id,
        )
    )
    await session.commit()

    response = await client.get(
        "/tracker/list", headers=_headers("kc-xss", "xss@example.com")
    )

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


async def test_the_account_page_names_the_broker_and_the_live_credential(
    client: AsyncClient, session: AsyncSession
) -> None:
    """`provider` is `keycloak` for every row, so it never told anyone how they
    actually signed in — nor which of two principals they are using now."""
    user = await make_user(session, email="acct@example.com", subject="kc-acct")
    identity = await session.scalar(
        select(Identity).where(Identity.provider_subject == "kc-acct")
    )
    identity.broker_alias = "github"
    await session.commit()

    response = await client.get(
        "/account", headers=_headers("kc-acct", user.primary_email)
    )

    assert response.status_code == 200
    assert "GitHub" in response.text
    assert "this session" in response.text


async def test_the_account_page_explains_an_unverified_address(
    client: AsyncClient, session: AsyncSession
) -> None:
    """The silent half of the sharing bug: an unverified address matches no
    invite, and nothing used to say so."""
    user = await make_user(
        session, email="unv@example.com", subject="kc-unv", verified=False
    )

    response = await client.get(
        "/account", headers=_headers("kc-unv", user.primary_email)
    )

    assert response.status_code == 200
    assert "cannot reach you" in response.text
