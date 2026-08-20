"""The yard-master API, end to end over ASGI.

The point of this suite is the negative case. Every read endpoint gets its own
"a stranger cannot" test, because the endpoints share helpers and a single
regression in one of them would otherwise be invisible: one test per endpoint,
no exceptions.

Two properties beyond access control are asserted here because nothing else
can:

* ``Tracker.device_key`` is the Traccar provisioning credential, so it must be
  absent from every list response and present only on a detail one. ``Tracker.id``
  is a surrogate and belongs in both.
* The admin bypass rests on the *token*, never on the proxy header or the
  session, so a forged ``groups`` claim in a token whose ``sub`` disagrees with
  the header must buy nothing.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from railroad_club.models.gtfs_static import GtfsStaticFeed, LoadStatus
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker
from railroad_club.models.tracker_rule import TrackerRule, TrackerRuleException

from tests.factories import PROVIDER, add_member, make_feed, make_user

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlmodel.ext.asyncio.session import AsyncSession

ADMIN_GROUP = "gtfs-admins"


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
    monkeypatch.setattr(database, "_engine", engine)

    from cafe_car.admin_main import create_admin_app

    app = create_admin_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    get_settings.cache_clear()


def _token(claims: dict) -> str:
    """An unsigned JWT. The app never verifies one; oauth2-proxy already did."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _headers(subject: str, email: str, *, groups: list[str] | None = None) -> dict:
    headers = {"X-Auth-Request-User": subject, "X-Auth-Request-Email": email}
    if groups is not None:
        headers["X-Auth-Request-Access-Token"] = _token(
            {"sub": subject, "email": email, "groups": groups}
        )
    return headers


@pytest.fixture
async def world(session: AsyncSession) -> dict:
    """One owner, one member, one stranger, and a feed with one of everything."""
    owner = await make_user(session, email="owner@example.com", subject="kc-owner")
    member = await make_user(session, email="member@example.com", subject="kc-member")
    stranger = await make_user(
        session, email="stranger@example.com", subject="kc-stranger"
    )

    static = GtfsStaticFeed(status=LoadStatus.success, timezone="America/New_York")
    session.add(static)
    await session.commit()

    feed = await make_feed(session, owner, "owner-feed")
    feed.gtfs_static_feed_id = static.id
    session.add(feed)
    await add_member(session, feed, member, added_by=owner)

    other = await make_feed(session, stranger, "stranger-feed")

    tracker = Tracker(
        device_key="lively-happy-otter", nickname="Otter", feed_id=feed.id
    )
    alert = ServiceAlert(
        feed_id=feed.id, header_text="Delays", description_text="Signal problem"
    )
    session.add_all([tracker, alert])
    await session.commit()

    entity = InformedEntity(service_alert_id=alert.id, route_id="R1")
    session.add(entity)
    await session.commit()

    return {
        "owner": owner,
        "member": member,
        "stranger": stranger,
        "feed": feed,
        "other": other,
        "tracker": tracker,
        "alert": alert,
        "entity": entity,
    }


OWNER = _headers("kc-owner", "owner@example.com")
MEMBER = _headers("kc-member", "member@example.com")
STRANGER = _headers("kc-stranger", "stranger@example.com")


class TestMe:
    async def test_reports_the_signed_in_user(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get("/api/me", headers=OWNER)

        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == world["owner"].id
        assert body["email"] == "owner@example.com"
        assert body["is_admin"] is False

    async def test_without_a_proxy_header_it_is_401(self, client: AsyncClient) -> None:
        response = await client.get("/api/me")

        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/json")


class TestFeedList:
    async def test_lists_owned_and_shared_feeds_only(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get("/api/feeds", headers=MEMBER)

        assert response.status_code == 200
        assert [f["feed_name"] for f in response.json()] == ["owner-feed"]

    async def test_carries_the_load_status(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get("/api/feeds", headers=OWNER)

        (feed,) = response.json()
        assert feed["load"]["status"] == "success"
        assert feed["load"]["timezone"] == "America/New_York"
        assert feed["is_owner"] is True
        assert feed["can_manage"] is True

    async def test_a_member_is_not_an_owner(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get("/api/feeds", headers=MEMBER)

        (feed,) = response.json()
        assert feed["is_owner"] is False
        assert feed["can_manage"] is False


class TestFeedDetail:
    async def test_a_stranger_cannot_read_a_feed(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(f"/api/feeds/{world['feed'].id}", headers=STRANGER)

        assert response.status_code == 404

    async def test_a_member_can(self, client: AsyncClient, world: dict) -> None:
        response = await client.get(f"/api/feeds/{world['feed'].id}", headers=MEMBER)

        assert response.status_code == 200
        assert response.json()["feed_name"] == "owner-feed"


class TestTrackers:
    async def test_a_stranger_cannot_list_trackers(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/trackers", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_a_stranger_cannot_read_a_tracker(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/trackers/{world['tracker'].id}", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_the_list_never_carries_the_credential(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/trackers", headers=OWNER
        )

        assert response.status_code == 200
        assert response.json() == [
            {
                "id": world["tracker"].id,
                "nickname": "Otter",
                "feed_id": world["feed"].id,
            }
        ]
        assert "lively-happy-otter" not in response.text

    async def test_the_detail_page_is_where_the_credential_lives(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/trackers/{world['tracker'].id}", headers=MEMBER
        )

        assert response.json() == {
            "id": world["tracker"].id,
            "nickname": "Otter",
            "feed_id": world["feed"].id,
            "device_key": "lively-happy-otter",
        }

    async def test_a_feed_never_returns_another_feeds_trackers(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        session.add(
            Tracker(
                device_key="quietly-brave-heron",
                nickname="Heron",
                feed_id=world["other"].id,
            )
        )
        await session.commit()

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/trackers", headers=OWNER
        )

        assert [t["nickname"] for t in response.json()] == ["Otter"]


class TestAssignments:
    """Expansion is over service dates, and it is scoped like everything else."""

    async def test_a_stranger_cannot_list_assignments(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-23",
            headers=STRANGER,
        )

        assert response.status_code == 404

    async def test_a_weekly_rule_expands_once_per_matching_day(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        session.add(
            TrackerRule(
                tracker_id=world["tracker"].id,
                trip_id="trip-1",
                monday=True,
                wednesday=True,
                start_date=date(2026, 1, 1),
                start_time=9 * 3600,
                end_time=17 * 3600,
            )
        )
        await session.commit()

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-23",
            headers=OWNER,
        )

        assert response.status_code == 200
        body = response.json()
        # 2026-08-17 is a Monday, 2026-08-19 the Wednesday after it.
        assert [a["service_date"] for a in body] == ["2026-08-17", "2026-08-19"]
        assert body[0]["tracker_nickname"] == "Otter"
        assert body[0]["trip_id"] == "trip-1"

    async def test_an_exception_removes_one_day_and_adds_another(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        rule = TrackerRule(
            tracker_id=world["tracker"].id,
            trip_id="trip-1",
            monday=True,
            start_date=date(2026, 1, 1),
            start_time=9 * 3600,
            end_time=17 * 3600,
        )
        session.add(rule)
        await session.flush()
        session.add_all(
            [
                TrackerRuleException(
                    rule_id=rule.id, date=date(2026, 8, 17), exception_type="removed"
                ),
                TrackerRuleException(
                    rule_id=rule.id, date=date(2026, 8, 21), exception_type="added"
                ),
            ]
        )
        await session.commit()

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-23",
            headers=OWNER,
        )

        assert [a["service_date"] for a in response.json()] == ["2026-08-21"]

    async def test_a_midnight_crossing_rule_lands_on_the_day_it_started(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        """One entry, on the start day, with an end_time past 86400 rather than
        a second entry on the following morning."""
        session.add(
            TrackerRule(
                tracker_id=world["tracker"].id,
                trip_id="owl-1",
                monday=True,
                start_date=date(2026, 1, 1),
                start_time=23 * 3600,
                end_time=25 * 3600,
            )
        )
        await session.commit()

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-18",
            headers=OWNER,
        )

        (assignment,) = response.json()
        assert assignment["service_date"] == "2026-08-17"
        assert assignment["end_time"] == 90000

    async def test_a_rule_outside_its_date_range_does_not_expand(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        session.add(
            TrackerRule(
                tracker_id=world["tracker"].id,
                trip_id="trip-1",
                monday=True,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 6, 30),
                start_time=9 * 3600,
                end_time=17 * 3600,
            )
        )
        await session.commit()

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-23",
            headers=OWNER,
        )

        assert response.json() == []

    async def test_a_backwards_range_is_rejected(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-23&to=2026-08-17",
            headers=OWNER,
        )

        assert response.status_code == 400

    async def test_an_unbounded_range_is_rejected(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2020-01-01&to=2030-01-01",
            headers=OWNER,
        )

        assert response.status_code == 400

    async def test_another_feeds_rules_never_appear(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        other_tracker = Tracker(
            device_key="quietly-brave-heron",
            nickname="Heron",
            feed_id=world["other"].id,
        )
        session.add(other_tracker)
        await session.flush()
        session.add(
            TrackerRule(
                tracker_id=other_tracker.id,
                trip_id="not-yours",
                monday=True,
                start_date=date(2026, 1, 1),
                start_time=9 * 3600,
                end_time=17 * 3600,
            )
        )
        await session.commit()

        response = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-23",
            headers=OWNER,
        )

        assert response.json() == []


class TestAlerts:
    async def test_a_stranger_cannot_list_alerts(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/alerts", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_a_stranger_cannot_read_an_alert(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/alerts/{world['alert'].id}", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_a_stranger_cannot_read_informed_entities(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/alerts/{world['alert'].id}/entities", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_a_member_reads_the_alert_and_its_entities(
        self, client: AsyncClient, world: dict
    ) -> None:
        listed = await client.get(
            f"/api/feeds/{world['feed'].id}/alerts", headers=MEMBER
        )
        detail = await client.get(f"/api/alerts/{world['alert'].id}", headers=MEMBER)

        assert [a["entity_count"] for a in listed.json()] == [1]
        assert [e["route_id"] for e in detail.json()["entities"]] == ["R1"]


class TestMembers:
    async def test_a_stranger_cannot_read_the_member_list(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/members", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_the_owner_leads_the_list(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/feeds/{world['feed'].id}/members", headers=OWNER
        )

        body = response.json()
        assert [m["email"] for m in body["members"]] == [
            "owner@example.com",
            "member@example.com",
        ]
        assert [m["is_owner"] for m in body["members"]] == [True, False]
        assert body["invites"] == []


class TestAdminBypass:
    async def test_an_admin_still_lists_only_their_own_feeds_by_default(
        self, client: AsyncClient, world: dict
    ) -> None:
        """The bypass is opt-in. A feed switcher showing every feed on the
        server would be useless to the one person who has to use it most."""
        headers = _headers("kc-member", "member@example.com", groups=[ADMIN_GROUP])

        response = await client.get("/api/feeds", headers=headers)

        assert [f["feed_name"] for f in response.json()] == ["owner-feed"]

    async def test_an_admin_can_ask_for_all_feeds(
        self, client: AsyncClient, world: dict
    ) -> None:
        headers = _headers("kc-member", "member@example.com", groups=[ADMIN_GROUP])

        response = await client.get("/api/feeds?all=1", headers=headers)

        assert sorted(f["feed_name"] for f in response.json()) == [
            "owner-feed",
            "stranger-feed",
        ]

    async def test_a_non_admin_asking_for_all_feeds_is_refused(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get("/api/feeds?all=1", headers=MEMBER)

        assert response.status_code == 403

    async def test_an_admin_can_read_a_feed_they_have_no_part_in(
        self, client: AsyncClient, world: dict
    ) -> None:
        headers = _headers("kc-member", "member@example.com", groups=[ADMIN_GROUP])

        response = await client.get(f"/api/feeds/{world['other'].id}", headers=headers)

        assert response.status_code == 200

    async def test_a_groups_claim_from_a_mismatched_token_buys_nothing(
        self, client: AsyncClient, world: dict
    ) -> None:
        """`verified_claims` discards a token whose `sub` disagrees with the
        proxy header, so a stolen or hand-rolled token for somebody else is not
        a route to the admin group."""
        headers = {
            "X-Auth-Request-User": "kc-member",
            "X-Auth-Request-Email": "member@example.com",
            "X-Auth-Request-Access-Token": _token(
                {"sub": "kc-somebody-else", "groups": [ADMIN_GROUP]}
            ),
        }

        response = await client.get(f"/api/feeds/{world['other'].id}", headers=headers)

        assert response.status_code == 404

    async def test_a_groups_header_is_not_a_token(
        self, client: AsyncClient, world: dict
    ) -> None:
        """Only the token can vouch for group membership; a header naming the
        group is not evidence of anything."""
        headers = {**MEMBER, "X-Auth-Request-Groups": ADMIN_GROUP}

        response = await client.get(f"/api/feeds/{world['other'].id}", headers=headers)

        assert response.status_code == 404


class TestCsrf:
    async def test_a_mutation_without_the_header_is_refused(
        self, client: AsyncClient, world: dict
    ) -> None:
        """Nothing under /api mutates yet, so this drives the dependency
        directly. The check is mounted on the router, so the route that phase 5
        adds inherits it whether or not it remembers to ask."""
        from fastapi import HTTPException
        from starlette.requests import Request

        from cafe_car.api.deps import require_csrf

        scope = {"type": "http", "method": "POST", "headers": []}
        with pytest.raises(HTTPException) as excinfo:
            await require_csrf(Request(scope))

        assert excinfo.value.status_code == 403

    async def test_a_safe_method_needs_no_header(self, client: AsyncClient) -> None:
        from starlette.requests import Request

        from cafe_car.api.deps import require_csrf

        assert (
            await require_csrf(
                Request({"type": "http", "method": "GET", "headers": []})
            )
            is None
        )

    async def test_a_read_works_without_the_header(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get("/api/me", headers=OWNER)

        assert response.status_code == 200


async def test_the_old_admin_still_answers(client: AsyncClient, world: dict) -> None:
    """The API router is registered before Admin mounts at "/". Registering it
    after would have swallowed it; registering it wrongly could swallow the
    admin. Both halves have to still work."""
    response = await client.get("/feed/list", headers=OWNER, follow_redirects=False)

    assert response.status_code == 200
