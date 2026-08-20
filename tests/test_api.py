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
from railroad_club.models.feed import Feed
from railroad_club.models.gtfs_static import GtfsStaticFeed, LoadStatus
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker
from railroad_club.models.tracker_rule import TrackerRule, TrackerRuleException
from sqlalchemy import func, select

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


# Every mutation carries the CSRF header; `require_csrf` is mounted on the
# whole router, so a POST without it never reaches a route.
WRITE = {"X-Yard-Master": "1"}

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
        tracker_id = world["tracker"].id
        rule = TrackerRule(
            tracker_id=tracker_id,
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


class TestRuleWrites:
    """The calendar's writes: rules, and the exceptions that bend them.

    A rule has no feed column, so every one of these scopes through the join to
    its tracker. That is the thing worth a test per endpoint: a rule id is a
    small integer and guessing one is trivial.
    """

    @staticmethod
    def _body(**overrides: object) -> dict:
        body = {
            "trip_id": "trip-1",
            "monday": True,
            "start_date": "2026-01-01",
            "start_time": 9 * 3600,
            "end_time": 17 * 3600,
        }
        body.update(overrides)
        return body

    async def _make_rule(self, session: AsyncSession, world: dict) -> TrackerRule:
        rule = TrackerRule(
            tracker_id=world["tracker"].id,
            trip_id="trip-1",
            monday=True,
            start_date=date(2026, 1, 1),
            start_time=9 * 3600,
            end_time=17 * 3600,
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
        return rule

    async def test_a_stranger_cannot_list_a_trackers_rules(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/trackers/{world['tracker'].id}/rules", headers=STRANGER
        )

        assert response.status_code == 404

    async def test_a_stranger_cannot_create_a_rule(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/trackers/{world['tracker'].id}/rules",
            json=self._body(),
            headers=STRANGER | WRITE,
        )

        assert response.status_code == 404

    async def test_a_stranger_cannot_read_edit_or_delete_a_rule(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        rule = await self._make_rule(session, world)

        assert (
            await client.get(f"/api/rules/{rule.id}", headers=STRANGER)
        ).status_code == 404
        assert (
            await client.patch(
                f"/api/rules/{rule.id}", json=self._body(), headers=STRANGER | WRITE
            )
        ).status_code == 404
        assert (
            await client.delete(f"/api/rules/{rule.id}", headers=STRANGER | WRITE)
        ).status_code == 404

    async def test_a_stranger_cannot_touch_the_exceptions(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        rule = await self._make_rule(session, world)

        response = await client.post(
            f"/api/rules/{rule.id}/exceptions",
            json={"date": "2026-08-17", "exception_type": "removed"},
            headers=STRANGER | WRITE,
        )

        assert response.status_code == 404

    async def test_a_member_creates_a_rule_and_it_expands(
        self, client: AsyncClient, world: dict
    ) -> None:
        created = await client.post(
            f"/api/trackers/{world['tracker'].id}/rules",
            json=self._body(),
            headers=MEMBER | WRITE,
        )

        assert created.status_code == 201
        assert created.json()["tracker_id"] == world["tracker"].id
        assert created.json()["exceptions"] == []

        expanded = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-23",
            headers=MEMBER,
        )
        assert [a["service_date"] for a in expanded.json()] == ["2026-08-17"]

    async def test_a_rule_with_no_weekday_is_allowed(
        self, client: AsyncClient, world: dict
    ) -> None:
        """A one-off assignment is every flag false plus one added date."""
        created = await client.post(
            f"/api/trackers/{world['tracker'].id}/rules",
            json=self._body(monday=False),
            headers=OWNER | WRITE,
        )
        assert created.status_code == 201
        rule_id = created.json()["id"]

        added = await client.post(
            f"/api/rules/{rule_id}/exceptions",
            json={"date": "2026-08-18", "exception_type": "added"},
            headers=OWNER | WRITE,
        )
        assert added.status_code == 201

        expanded = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-23",
            headers=OWNER,
        )
        assert [a["service_date"] for a in expanded.json()] == ["2026-08-18"]

    async def test_a_backwards_window_is_rejected(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/trackers/{world['tracker'].id}/rules",
            json=self._body(start_time=17 * 3600, end_time=9 * 3600),
            headers=OWNER | WRITE,
        )

        assert response.status_code == 422

    async def test_a_window_crossing_midnight_is_not(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/trackers/{world['tracker'].id}/rules",
            json=self._body(start_time=23 * 3600, end_time=25 * 3600),
            headers=OWNER | WRITE,
        )

        assert response.status_code == 201
        assert response.json()["end_time"] == 90000

    async def test_an_end_date_before_the_start_is_rejected(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/trackers/{world['tracker'].id}/rules",
            json=self._body(end_date="2025-12-31"),
            headers=OWNER | WRITE,
        )

        assert response.status_code == 422

    async def test_a_patch_replaces_the_recurrence_and_keeps_the_exceptions(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        rule = await self._make_rule(session, world)
        await client.post(
            f"/api/rules/{rule.id}/exceptions",
            json={"date": "2026-08-17", "exception_type": "removed"},
            headers=OWNER | WRITE,
        )

        response = await client.patch(
            f"/api/rules/{rule.id}",
            json=self._body(monday=False, tuesday=True, trip_id="trip-2"),
            headers=OWNER | WRITE,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["monday"] is False
        assert body["tuesday"] is True
        assert body["trip_id"] == "trip-2"
        assert [e["date"] for e in body["exceptions"]] == ["2026-08-17"]

    async def test_a_patch_cannot_move_a_rule_to_another_tracker(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        rule = await self._make_rule(session, world)
        other_tracker = Tracker(
            device_key="quietly-brave-heron",
            nickname="Heron",
            feed_id=world["other"].id,
        )
        session.add(other_tracker)
        await session.commit()

        response = await client.patch(
            f"/api/rules/{rule.id}",
            json=self._body() | {"tracker_id": other_tracker.id},
            headers=OWNER | WRITE,
        )

        assert response.json()["tracker_id"] == world["tracker"].id

    async def test_writing_the_same_date_twice_replaces_the_exception(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        rule = await self._make_rule(session, world)
        first = await client.post(
            f"/api/rules/{rule.id}/exceptions",
            json={"date": "2026-08-17", "exception_type": "removed"},
            headers=OWNER | WRITE,
        )
        second = await client.post(
            f"/api/rules/{rule.id}/exceptions",
            json={"date": "2026-08-17", "exception_type": "added"},
            headers=OWNER | WRITE,
        )

        assert second.status_code == 201
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["exception_type"] == "added"

    async def test_deleting_an_exception_puts_the_day_back(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        rule = await self._make_rule(session, world)
        created = await client.post(
            f"/api/rules/{rule.id}/exceptions",
            json={"date": "2026-08-17", "exception_type": "removed"},
            headers=OWNER | WRITE,
        )

        removed = await client.delete(
            f"/api/rules/{rule.id}/exceptions/{created.json()['id']}",
            headers=OWNER | WRITE,
        )
        assert removed.status_code == 204

        expanded = await client.get(
            f"/api/feeds/{world['feed'].id}/assignments?from=2026-08-17&to=2026-08-23",
            headers=OWNER,
        )
        assert [a["service_date"] for a in expanded.json()] == ["2026-08-17"]

    async def test_deleting_a_rule_takes_its_exceptions_with_it(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        rule = await self._make_rule(session, world)
        await client.post(
            f"/api/rules/{rule.id}/exceptions",
            json={"date": "2026-08-17", "exception_type": "removed"},
            headers=OWNER | WRITE,
        )

        response = await client.delete(f"/api/rules/{rule.id}", headers=OWNER | WRITE)

        assert response.status_code == 204
        remaining = await session.execute(
            select(func.count()).select_from(TrackerRuleException)
        )
        assert remaining.scalar() == 0

    async def test_a_write_without_the_csrf_header_is_refused(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/trackers/{world['tracker'].id}/rules",
            json=self._body(),
            headers=OWNER,
        )

        assert response.status_code == 403


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
        """The check is mounted on the router rather than on the route, so a
        mutation added later inherits it whether or not it remembers to ask."""
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/reload", headers=OWNER
        )

        assert response.status_code == 403

    async def test_the_dependency_refuses_any_unsafe_method(self) -> None:
        from fastapi import HTTPException
        from starlette.requests import Request

        from cafe_car.api.deps import require_csrf

        scope = {"type": "http", "method": "DELETE", "headers": []}
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


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Records what was queued instead of talking to a broker that is not up."""
    queued: list[int] = []
    monkeypatch.setattr("cafe_car.api.feeds.request_feed_load", queued.append)
    return queued


class TestCreateFeed:
    async def test_the_creator_owns_it_and_a_load_is_queued(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.post(
            "/api/feeds",
            headers={**MEMBER, **WRITE},
            json={
                "feed_name": "brand-new",
                "static_feed_url": "https://example.com/gtfs.zip",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["feed_name"] == "brand-new"
        assert body["owner_id"] == world["member"].id
        assert body["is_owner"] is True
        assert body["can_manage"] is True
        # No `gtfs_static_feed` row exists yet, and the client must not read
        # that as "pending".
        assert body["load"] is None
        assert loads == [body["id"]]

    async def test_the_new_feed_is_in_the_creators_own_list(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        await client.post(
            "/api/feeds",
            headers={**MEMBER, **WRITE},
            json={
                "feed_name": "brand-new",
                "static_feed_url": "https://example.com/gtfs.zip",
            },
        )

        response = await client.get("/api/feeds", headers=MEMBER)

        assert "brand-new" in [f["feed_name"] for f in response.json()]

    async def test_a_taken_name_is_a_conflict_not_a_crash(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.post(
            "/api/feeds",
            headers={**MEMBER, **WRITE},
            json={
                "feed_name": world["feed"].feed_name,
                "static_feed_url": "https://example.com/gtfs.zip",
            },
        )

        assert response.status_code == 409
        assert loads == []

    @pytest.mark.parametrize(
        "name", ["Uppercase", "ab", "has space", "1leading-digit", ""]
    )
    async def test_a_malformed_name_is_refused(
        self, client: AsyncClient, world: dict, loads: list[int], name: str
    ) -> None:
        response = await client.post(
            "/api/feeds",
            headers={**MEMBER, **WRITE},
            json={
                "feed_name": name,
                "static_feed_url": "https://example.com/gtfs.zip",
            },
        )

        assert response.status_code == 422

    async def test_a_url_that_is_not_a_url_is_refused(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.post(
            "/api/feeds",
            headers={**MEMBER, **WRITE},
            json={"feed_name": "brand-new", "static_feed_url": "not-a-url"},
        )

        assert response.status_code == 422

    async def test_a_url_is_stored_exactly_as_typed(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        """`AnyHttpUrl` would append a trailing slash to a bare host, which
        rewrites the URL somebody pasted."""
        response = await client.post(
            "/api/feeds",
            headers={**MEMBER, **WRITE},
            json={"feed_name": "brand-new", "static_feed_url": "https://example.com"},
        )

        assert response.json()["static_feed_url"] == "https://example.com"

    async def test_a_stranger_cannot_create_a_feed_they_do_not_own(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        """Ownership is the caller, never the body: an `owner_id` in the
        payload has to buy nothing."""
        response = await client.post(
            "/api/feeds",
            headers={**MEMBER, **WRITE},
            json={
                "feed_name": "brand-new",
                "static_feed_url": "https://example.com/gtfs.zip",
                "owner_id": world["stranger"].id,
            },
        )

        assert response.json()["owner_id"] == world["member"].id


class TestReloadFeed:
    async def test_the_owner_can_queue_a_reload(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/reload", headers={**OWNER, **WRITE}
        )

        assert response.status_code == 202
        assert loads == [world["feed"].id]

    async def test_a_member_can_too(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        """Re-downloading the schedule a member already works against is not an
        owner-only act, matching the admin reload button this replaces."""
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/reload", headers={**MEMBER, **WRITE}
        )

        assert response.status_code == 202

    async def test_a_stranger_cannot(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/reload", headers={**STRANGER, **WRITE}
        )

        assert response.status_code == 404
        assert loads == []


async def test_the_old_admin_still_answers(client: AsyncClient, world: dict) -> None:
    """The API router is registered before Admin mounts at "/". Registering it
    after would have swallowed it; registering it wrongly could swallow the
    admin. Both halves have to still work."""
    response = await client.get("/feed/list", headers=OWNER, follow_redirects=False)

    assert response.status_code == 200


@pytest.fixture
def traccar(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Records provisioning instead of talking to a Traccar that is not up.

    Both halves are best-effort in production, so a test that did not patch
    them would still pass - slowly, and while asserting nothing about what was
    provisioned. Recording them is what lets the delete tests say the device
    was retired with the credential and not with the surrogate.
    """
    calls: dict[str, list] = {"provisioned": [], "retired": []}

    async def provision(nickname: str, device_key: str) -> None:
        calls["provisioned"].append((nickname, device_key))

    async def retire(device_key: str) -> None:
        calls["retired"].append(device_key)

    monkeypatch.setattr("cafe_car.api.trackers.provision_device", provision)
    monkeypatch.setattr("cafe_car.api.trackers.retire_device", retire)
    monkeypatch.setattr("cafe_car.api.feeds.retire_device", retire)
    return calls


class TestUpdateFeed:
    async def test_a_member_can_rename_it(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.patch(
            f"/api/feeds/{world['feed'].id}",
            headers={**MEMBER, **WRITE},
            json={"feed_name": "renamed-feed"},
        )

        assert response.status_code == 200
        assert response.json()["feed_name"] == "renamed-feed"
        # The name is in every public GTFS-RT URL, so those move with it.
        assert "renamed-feed" in response.json()["vehicle_positions_url"]

    async def test_a_taken_name_is_a_conflict(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.patch(
            f"/api/feeds/{world['feed'].id}",
            headers={**OWNER, **WRITE},
            json={"feed_name": world["other"].feed_name},
        )

        assert response.status_code == 409

    async def test_a_malformed_name_is_refused(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.patch(
            f"/api/feeds/{world['feed'].id}",
            headers={**OWNER, **WRITE},
            json={"feed_name": "Not A Feed Name"},
        )

        assert response.status_code == 422

    async def test_repointing_the_url_queues_a_reload(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        """A different zip is a different schedule, and waiting for the retry
        timer to notice would leave the feed serving the old one."""
        response = await client.patch(
            f"/api/feeds/{world['feed'].id}",
            headers={**OWNER, **WRITE},
            json={"static_feed_url": "https://example.com/other.zip"},
        )

        assert response.status_code == 200
        assert loads == [world["feed"].id]

    async def test_renaming_alone_queues_nothing(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        await client.patch(
            f"/api/feeds/{world['feed'].id}",
            headers={**OWNER, **WRITE},
            json={"feed_name": "renamed-feed"},
        )

        assert loads == []

    async def test_ownership_is_not_settable_from_the_body(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.patch(
            f"/api/feeds/{world['feed'].id}",
            headers={**MEMBER, **WRITE},
            json={"owner_id": world["member"].id},
        )

        assert response.json()["owner_id"] == world["owner"].id

    async def test_a_stranger_cannot(
        self, client: AsyncClient, world: dict, loads: list[int]
    ) -> None:
        response = await client.patch(
            f"/api/feeds/{world['feed'].id}",
            headers={**STRANGER, **WRITE},
            json={"feed_name": "stolen-feed"},
        )

        assert response.status_code == 404


class TestDeleteFeed:
    async def test_the_owner_can_and_it_takes_the_feed_with_it(
        self, client: AsyncClient, world: dict, session: AsyncSession, traccar: dict
    ) -> None:
        feed_id = world["feed"].id

        response = await client.delete(
            f"/api/feeds/{feed_id}", headers={**OWNER, **WRITE}
        )

        assert response.status_code == 204
        session.expire_all()
        # Trackers and alerts do not cascade in the model, so a feed delete that
        # did not clear them would have raised a foreign-key error instead.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Tracker)
                .where(Tracker.feed_id == feed_id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ServiceAlert)
                .where(ServiceAlert.feed_id == feed_id)
            )
            == 0
        )
        assert await session.get(Feed, feed_id) is None

    async def test_it_retires_the_traccar_devices(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        await client.delete(
            f"/api/feeds/{world['feed'].id}", headers={**OWNER, **WRITE}
        )

        assert traccar["retired"] == [world["tracker"].device_key]

    async def test_a_member_cannot(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.delete(
            f"/api/feeds/{world['feed'].id}", headers={**MEMBER, **WRITE}
        )

        assert response.status_code == 403

    async def test_a_stranger_gets_404_not_403(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        """A 403 would confirm the id exists to somebody who cannot see it."""
        response = await client.delete(
            f"/api/feeds/{world['feed'].id}", headers={**STRANGER, **WRITE}
        )

        assert response.status_code == 404


class TestTransferFeed:
    async def test_the_owner_hands_it_to_a_member_and_stays_on(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/transfer",
            headers={**OWNER, **WRITE},
            json={"new_owner_id": world["member"].id},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["owner_id"] == world["member"].id
        # The person who gave it away keeps their access, as a member.
        assert body["is_owner"] is False

        listed = await client.get("/api/feeds", headers=OWNER)
        assert world["feed"].feed_name in [f["feed_name"] for f in listed.json()]

    async def test_it_can_only_go_to_an_existing_member(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/transfer",
            headers={**OWNER, **WRITE},
            json={"new_owner_id": world["stranger"].id},
        )

        assert response.status_code == 400

    async def test_a_member_cannot_transfer_it_to_themselves(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/transfer",
            headers={**MEMBER, **WRITE},
            json={"new_owner_id": world["member"].id},
        )

        assert response.status_code == 403

    async def test_a_stranger_cannot(self, client: AsyncClient, world: dict) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/transfer",
            headers={**STRANGER, **WRITE},
            json={"new_owner_id": world["stranger"].id},
        )

        assert response.status_code == 404


class TestCreateTracker:
    async def test_a_member_can_and_gets_the_credential_back(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        """Whoever just made a tracker is about to provision it, so the create
        response is the detail form. Every *list* response stays without it."""
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/trackers",
            headers={**MEMBER, **WRITE},
            json={"nickname": "Badger"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["nickname"] == "Badger"
        assert body["device_key"]
        assert traccar["provisioned"] == [("Badger", body["device_key"])]

    async def test_a_supplied_credential_is_used_as_typed(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/trackers",
            headers={**OWNER, **WRITE},
            json={"nickname": "Badger", "device_key": "quietly-brave-mole"},
        )

        assert response.json()["device_key"] == "quietly-brave-mole"

    async def test_a_repeated_nickname_is_a_conflict(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/trackers",
            headers={**OWNER, **WRITE},
            json={"nickname": world["tracker"].nickname},
        )

        assert response.status_code == 409
        assert traccar["provisioned"] == []

    async def test_the_same_nickname_on_another_feed_is_fine(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        """The constraint is `(feed_id, nickname)`: two agencies may both have
        a tracker called `1`."""
        response = await client.post(
            f"/api/feeds/{world['other'].id}/trackers",
            headers={**STRANGER, **WRITE},
            json={"nickname": world["tracker"].nickname},
        )

        assert response.status_code == 201

    async def test_a_blank_nickname_is_refused(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/trackers",
            headers={**OWNER, **WRITE},
            json={"nickname": "   "},
        )

        assert response.status_code == 422

    async def test_a_stranger_cannot(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/trackers",
            headers={**STRANGER, **WRITE},
            json={"nickname": "Badger"},
        )

        assert response.status_code == 404


class TestBulkCreateTrackers:
    async def test_it_numbers_from_the_prefix(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/trackers/bulk",
            headers={**OWNER, **WRITE},
            json={"prefix": "bus-", "count": 3},
        )

        assert response.status_code == 201
        assert [t["nickname"] for t in response.json()] == ["bus-1", "bus-2", "bus-3"]
        # The summary form: a device key is fetched per tracker, on its page.
        assert "device_key" not in response.json()[0]

    async def test_running_it_twice_extends_the_fleet(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        """`(feed_id, nickname)` is unique, and starting from 1 again is the
        easiest way to trip over it."""
        for _ in range(2):
            response = await client.post(
                f"/api/feeds/{world['feed'].id}/trackers/bulk",
                headers={**OWNER, **WRITE},
                json={"prefix": "bus-", "count": 2},
            )
            assert response.status_code == 201

        assert [t["nickname"] for t in response.json()] == ["bus-3", "bus-4"]

    async def test_every_one_is_provisioned(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        await client.post(
            f"/api/feeds/{world['feed'].id}/trackers/bulk",
            headers={**OWNER, **WRITE},
            json={"prefix": "bus-", "count": 2},
        )

        assert [name for name, _ in traccar["provisioned"]] == ["bus-1", "bus-2"]

    async def test_an_unbounded_count_is_refused(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/trackers/bulk",
            headers={**OWNER, **WRITE},
            json={"prefix": "bus-", "count": 5000},
        )

        assert response.status_code == 422

    async def test_a_stranger_cannot(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/trackers/bulk",
            headers={**STRANGER, **WRITE},
            json={"prefix": "bus-", "count": 1},
        )

        assert response.status_code == 404


class TestUpdateTracker:
    async def test_a_member_can_rename_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.patch(
            f"/api/trackers/{world['tracker'].id}",
            headers={**MEMBER, **WRITE},
            json={"nickname": "Renamed"},
        )

        assert response.status_code == 200
        assert response.json()["nickname"] == "Renamed"
        # The surrogate is what links point at, so a rename never moves one.
        assert response.json()["id"] == world["tracker"].id

    async def test_the_rename_never_returns_the_credential(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.patch(
            f"/api/trackers/{world['tracker'].id}",
            headers={**OWNER, **WRITE},
            json={"nickname": "Renamed"},
        )

        assert "device_key" not in response.json()

    async def test_the_credential_is_not_settable(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        """It is baked into the provisioned Traccar device, so a body that
        carries one has to buy nothing."""
        tracker_id = world["tracker"].id
        await client.patch(
            f"/api/trackers/{tracker_id}",
            headers={**OWNER, **WRITE},
            json={"nickname": "Renamed", "device_key": "stolen-key"},
        )

        session.expire_all()
        tracker = await session.get(Tracker, tracker_id)
        assert tracker.device_key == "lively-happy-otter"

    async def test_a_colliding_rename_is_a_conflict(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        other = Tracker(nickname="Badger", feed_id=world["feed"].id)
        session.add(other)
        await session.commit()

        response = await client.patch(
            f"/api/trackers/{other.id}",
            headers={**OWNER, **WRITE},
            json={"nickname": world["tracker"].nickname},
        )

        assert response.status_code == 409

    async def test_a_stranger_cannot(self, client: AsyncClient, world: dict) -> None:
        response = await client.patch(
            f"/api/trackers/{world['tracker'].id}",
            headers={**STRANGER, **WRITE},
            json={"nickname": "Renamed"},
        )

        assert response.status_code == 404


class TestDeleteTracker:
    async def test_it_takes_the_rules_and_the_device_with_it(
        self, client: AsyncClient, world: dict, session: AsyncSession, traccar: dict
    ) -> None:
        tracker_id = world["tracker"].id
        rule = TrackerRule(
            tracker_id=tracker_id,
            trip_id="T1",
            monday=True,
            start_date=date(2026, 1, 1),
            start_time=3600,
            end_time=7200,
        )
        session.add(rule)
        await session.commit()
        session.add(
            TrackerRuleException(
                rule_id=rule.id, date=date(2026, 1, 5), exception_type="removed"
            )
        )
        await session.commit()

        response = await client.delete(
            f"/api/trackers/{tracker_id}", headers={**OWNER, **WRITE}
        )

        assert response.status_code == 204
        session.expire_all()
        assert await session.get(Tracker, tracker_id) is None
        assert await session.scalar(select(func.count()).select_from(TrackerRule)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(TrackerRuleException))
            == 0
        )
        # Retired by credential, which is the Traccar `uniqueId`, never by the
        # surrogate.
        assert traccar["retired"] == ["lively-happy-otter"]

    async def test_a_member_can(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.delete(
            f"/api/trackers/{world['tracker'].id}", headers={**MEMBER, **WRITE}
        )

        assert response.status_code == 204

    async def test_a_stranger_cannot(
        self, client: AsyncClient, world: dict, traccar: dict
    ) -> None:
        response = await client.delete(
            f"/api/trackers/{world['tracker'].id}", headers={**STRANGER, **WRITE}
        )

        assert response.status_code == 404
        assert traccar["retired"] == []


class TestProvisioning:
    async def test_it_carries_the_credential_and_its_qr(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/trackers/{world['tracker'].id}/provisioning", headers=OWNER
        )

        assert response.status_code == 200
        body = response.json()
        assert body["device_key"] == "lively-happy-otter"
        assert "lively-happy-otter" in body["config_url"]
        assert body["qr_svg"].lstrip().startswith("<?xml")

    async def test_a_stranger_cannot_read_it(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.get(
            f"/api/trackers/{world['tracker'].id}/provisioning", headers=STRANGER
        )

        assert response.status_code == 404


class TestWriteAlerts:
    async def test_a_member_can_publish_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/alerts",
            headers={**MEMBER, **WRITE},
            json={
                "header_text": "Bridge out",
                "description_text": "Use the shuttle",
                "cause": "CONSTRUCTION",
                "effect": "DETOUR",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["header_text"] == "Bridge out"
        # No entities means the alert applies to the whole feed, which is a
        # real thing to publish.
        assert body["entities"] == []

    async def test_a_cause_outside_the_enumeration_is_refused(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/alerts",
            headers={**OWNER, **WRITE},
            json={
                "header_text": "Bridge out",
                "description_text": "Use the shuttle",
                "cause": "BADGERS",
            },
        )

        assert response.status_code == 422

    async def test_a_window_that_ends_before_it_starts_is_refused(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/alerts",
            headers={**OWNER, **WRITE},
            json={
                "header_text": "Bridge out",
                "description_text": "Use the shuttle",
                "active_period_start": "2026-02-01T10:00:00",
                "active_period_end": "2026-01-01T10:00:00",
            },
        )

        assert response.status_code == 422

    async def test_an_omitted_field_is_a_cleared_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        """The form is a whole alert, so "the URL is now blank" has to be
        expressible."""
        alert_id = world["alert"].id
        await client.patch(
            f"/api/alerts/{alert_id}",
            headers={**OWNER, **WRITE},
            json={
                "header_text": "Delays",
                "description_text": "Signal problem",
                "url": "https://example.com/notice",
            },
        )

        response = await client.patch(
            f"/api/alerts/{alert_id}",
            headers={**OWNER, **WRITE},
            json={"header_text": "Delays", "description_text": "Signal problem"},
        )

        assert response.json()["url"] is None

    async def test_the_patch_carries_the_entities_back(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.patch(
            f"/api/alerts/{world['alert'].id}",
            headers={**OWNER, **WRITE},
            json={"header_text": "Still delayed", "description_text": "Signal problem"},
        )

        assert [e["route_id"] for e in response.json()["entities"]] == ["R1"]

    async def test_deleting_one_takes_its_entities(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        response = await client.delete(
            f"/api/alerts/{world['alert'].id}", headers={**OWNER, **WRITE}
        )

        assert response.status_code == 204
        session.expire_all()
        assert (
            await session.scalar(select(func.count()).select_from(InformedEntity)) == 0
        )

    async def test_a_stranger_cannot_create_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/alerts",
            headers={**STRANGER, **WRITE},
            json={"header_text": "Fake", "description_text": "Fake"},
        )

        assert response.status_code == 404

    async def test_a_stranger_cannot_edit_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.patch(
            f"/api/alerts/{world['alert'].id}",
            headers={**STRANGER, **WRITE},
            json={"header_text": "Fake", "description_text": "Fake"},
        )

        assert response.status_code == 404

    async def test_a_stranger_cannot_delete_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.delete(
            f"/api/alerts/{world['alert'].id}", headers={**STRANGER, **WRITE}
        )

        assert response.status_code == 404


class TestWriteInformedEntities:
    async def test_a_member_can_add_one(self, client: AsyncClient, world: dict) -> None:
        response = await client.post(
            f"/api/alerts/{world['alert'].id}/entities",
            headers={**MEMBER, **WRITE},
            json={"stop_id": "S1"},
        )

        assert response.status_code == 201
        assert response.json()["stop_id"] == "S1"

    async def test_a_selector_that_names_nothing_is_refused(
        self, client: AsyncClient, world: dict
    ) -> None:
        """The column carries the same check constraint; catching it here is
        what makes it a field error instead of an IntegrityError."""
        response = await client.post(
            f"/api/alerts/{world['alert'].id}/entities",
            headers={**OWNER, **WRITE},
            json={"trip_start_date": "20260101"},
        )

        assert response.status_code == 422

    async def test_blank_strings_are_not_specifiers(
        self, client: AsyncClient, world: dict
    ) -> None:
        """The form posts an empty string for every field nobody filled in."""
        response = await client.post(
            f"/api/alerts/{world['alert'].id}/entities",
            headers={**OWNER, **WRITE},
            json={"route_id": "", "stop_id": ""},
        )

        assert response.status_code == 422

    async def test_a_direction_needs_a_route(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/alerts/{world['alert'].id}/entities",
            headers={**OWNER, **WRITE},
            json={"direction_id": 0},
        )

        assert response.status_code == 422

    async def test_an_id_the_schedule_does_not_contain_is_still_accepted(
        self, client: AsyncClient, world: dict
    ) -> None:
        """The zip is parsed in the browser and can be loaded after the alert;
        an unknown id is a warning for the form to draw, not a refusal."""
        response = await client.post(
            f"/api/alerts/{world['alert'].id}/entities",
            headers={**OWNER, **WRITE},
            json={"route_id": "no-such-route"},
        )

        assert response.status_code == 201

    async def test_one_is_deleted_only_through_its_own_alert(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        other = ServiceAlert(
            feed_id=world["feed"].id, header_text="Other", description_text="Other"
        )
        session.add(other)
        await session.commit()

        response = await client.delete(
            f"/api/alerts/{other.id}/entities/{world['entity'].id}",
            headers={**OWNER, **WRITE},
        )

        assert response.status_code == 404

    async def test_the_owner_can_delete_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.delete(
            f"/api/alerts/{world['alert'].id}/entities/{world['entity'].id}",
            headers={**OWNER, **WRITE},
        )

        assert response.status_code == 204

    async def test_a_stranger_cannot_add_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/alerts/{world['alert'].id}/entities",
            headers={**STRANGER, **WRITE},
            json={"stop_id": "S1"},
        )

        assert response.status_code == 404

    async def test_a_stranger_cannot_delete_one(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.delete(
            f"/api/alerts/{world['alert'].id}/entities/{world['entity'].id}",
            headers={**STRANGER, **WRITE},
        )

        assert response.status_code == 404


class TestWriteMembers:
    async def test_the_owner_adds_somebody_who_has_an_account(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/members",
            headers={**OWNER, **WRITE},
            json={"email": "stranger@example.com"},
        )

        assert response.status_code == 201
        assert response.json()["kind"] == "member"

        people = await client.get(
            f"/api/feeds/{world['feed'].id}/members", headers=OWNER
        )
        assert world["stranger"].id in [m["user_id"] for m in people.json()["members"]]

    async def test_an_unknown_address_becomes_an_invite(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/members",
            headers={**OWNER, **WRITE},
            json={"email": "nobody@example.com"},
        )

        assert response.json()["kind"] == "invited"

        people = await client.get(
            f"/api/feeds/{world['feed'].id}/members", headers=OWNER
        )
        assert [i["email"] for i in people.json()["invites"]] == ["nobody@example.com"]

    async def test_an_unverified_address_is_only_ever_an_invite(
        self, client: AsyncClient, world: dict, session: AsyncSession
    ) -> None:
        """Matching on a merely-claimed address is an account-takeover
        primitive, so an account whose provider never vouched for the address
        does not receive the feed."""
        await make_user(
            session, email="unverified@example.com", subject="kc-unv", verified=False
        )

        response = await client.post(
            f"/api/feeds/{world['feed'].id}/members",
            headers={**OWNER, **WRITE},
            json={"email": "unverified@example.com"},
        )

        assert response.json()["kind"] == "invited"

    async def test_a_member_cannot_add_anybody(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/members",
            headers={**MEMBER, **WRITE},
            json={"email": "stranger@example.com"},
        )

        assert response.status_code == 403

    async def test_a_stranger_cannot(self, client: AsyncClient, world: dict) -> None:
        response = await client.post(
            f"/api/feeds/{world['feed'].id}/members",
            headers={**STRANGER, **WRITE},
            json={"email": "stranger@example.com"},
        )

        assert response.status_code == 404

    async def test_the_owner_removes_a_member(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.delete(
            f"/api/feeds/{world['feed'].id}/members/{world['member'].id}",
            headers={**OWNER, **WRITE},
        )

        assert response.status_code == 204

        listed = await client.get("/api/feeds", headers=MEMBER)
        assert listed.json() == []

    async def test_the_owner_cannot_be_removed(
        self, client: AsyncClient, world: dict
    ) -> None:
        """They are not a `feed_member` row at all, so this would silently do
        nothing; saying so points at `/transfer`, which is what was meant."""
        response = await client.delete(
            f"/api/feeds/{world['feed'].id}/members/{world['owner'].id}",
            headers={**OWNER, **WRITE},
        )

        assert response.status_code == 400

    async def test_a_member_cannot_remove_another(
        self, client: AsyncClient, world: dict
    ) -> None:
        response = await client.delete(
            f"/api/feeds/{world['feed'].id}/members/{world['member'].id}",
            headers={**MEMBER, **WRITE},
        )

        assert response.status_code == 403

    async def test_the_owner_revokes_an_invite(
        self, client: AsyncClient, world: dict
    ) -> None:
        await client.post(
            f"/api/feeds/{world['feed'].id}/members",
            headers={**OWNER, **WRITE},
            json={"email": "nobody@example.com"},
        )
        people = await client.get(
            f"/api/feeds/{world['feed'].id}/members", headers=OWNER
        )
        invite_id = people.json()["invites"][0]["id"]

        response = await client.delete(
            f"/api/feeds/{world['feed'].id}/invites/{invite_id}",
            headers={**OWNER, **WRITE},
        )

        assert response.status_code == 204
        people = await client.get(
            f"/api/feeds/{world['feed'].id}/members", headers=OWNER
        )
        assert people.json()["invites"] == []

    async def test_an_invite_is_revoked_only_through_its_own_feed(
        self, client: AsyncClient, world: dict
    ) -> None:
        await client.post(
            f"/api/feeds/{world['feed'].id}/members",
            headers={**OWNER, **WRITE},
            json={"email": "nobody@example.com"},
        )
        people = await client.get(
            f"/api/feeds/{world['feed'].id}/members", headers=OWNER
        )
        invite_id = people.json()["invites"][0]["id"]

        # The stranger owns `other`, so this is an owner acting on their own
        # feed with somebody else's invite id.
        await client.delete(
            f"/api/feeds/{world['other'].id}/invites/{invite_id}",
            headers={**STRANGER, **WRITE},
        )

        people = await client.get(
            f"/api/feeds/{world['feed'].id}/members", headers=OWNER
        )
        assert len(people.json()["invites"]) == 1
