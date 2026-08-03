"""Who can see what.

``accessible_feed_ids`` is the single definition of "may touch this feed".
Trackers, tracker rules, service alerts and informed entities all scope
*through* their feed, so these tests walk each of the five entity types to
prove the inheritance is real rather than assumed.
"""

from __future__ import annotations

from datetime import time
from typing import TYPE_CHECKING

import pytest
from railroad_club.models.feed import Feed
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker
from railroad_club.models.tracker_rule import TrackerRule
from sqlalchemy import select

from cafe_car.admin.access import accessible_feed_ids, owned_feed_ids
from tests.factories import add_member, make_feed, make_user

if TYPE_CHECKING:
    from railroad_club.models.user import User
    from sqlmodel.ext.asyncio.session import AsyncSession


@pytest.fixture
async def world(session: AsyncSession) -> dict:
    """One owner, one member, one stranger, and a feed with one of everything."""
    owner = await make_user(session, email="owner@example.com")
    member = await make_user(session, email="member@example.com")
    stranger = await make_user(session, email="stranger@example.com")
    feed = await make_feed(session, owner, "shared-feed")
    await add_member(session, feed, member, added_by=owner)

    tracker = Tracker(id="lively-happy-otter", nickname="Otter", feed_id=feed.id)
    alert = ServiceAlert(
        feed_id=feed.id, header_text="Delays", description_text="Signal problem"
    )
    session.add_all([tracker, alert])
    await session.commit()

    rule = TrackerRule(
        tracker_id=tracker.id,
        trip_id="trip-1",
        monday=True,
        start_time=time(9, 0),
        end_time=time(17, 0),
    )
    entity = InformedEntity(service_alert_id=alert.id, route_id="route-1")
    session.add_all([rule, entity])
    await session.commit()

    return {
        "owner": owner,
        "member": member,
        "stranger": stranger,
        "feed": feed,
        "tracker": tracker,
        "alert": alert,
        "rule": rule,
        "entity": entity,
    }


async def _visible_feed_ids(session: AsyncSession, user: User) -> list[int]:
    result = await session.execute(
        select(Feed.id).where(Feed.id.in_(accessible_feed_ids(user.id)))
    )
    return list(result.scalars().all())


class TestFeeds:
    async def test_owner_sees_their_feed(
        self, session: AsyncSession, world: dict
    ) -> None:
        assert await _visible_feed_ids(session, world["owner"]) == [world["feed"].id]

    async def test_member_sees_the_shared_feed(
        self, session: AsyncSession, world: dict
    ) -> None:
        assert await _visible_feed_ids(session, world["member"]) == [world["feed"].id]

    async def test_stranger_sees_nothing(
        self, session: AsyncSession, world: dict
    ) -> None:
        assert await _visible_feed_ids(session, world["stranger"]) == []

    async def test_the_unauthenticated_zero_sees_nothing(
        self, session: AsyncSession, world: dict
    ) -> None:
        """Routes fall back to user id 0 when there is no proxy header; it must
        match no rows."""
        result = await session.execute(
            select(Feed.id).where(Feed.id.in_(accessible_feed_ids(0)))
        )
        assert list(result.scalars().all()) == []


class TestOwnedOnly:
    async def test_owner_only_for_the_owner(
        self, session: AsyncSession, world: dict
    ) -> None:
        result = await session.execute(
            select(Feed.id).where(Feed.id.in_(owned_feed_ids(world["owner"].id)))
        )
        assert list(result.scalars().all()) == [world["feed"].id]

    async def test_a_member_owns_nothing(
        self, session: AsyncSession, world: dict
    ) -> None:
        result = await session.execute(
            select(Feed.id).where(Feed.id.in_(owned_feed_ids(world["member"].id)))
        )
        assert list(result.scalars().all()) == []


class TestScopingInheritsThroughTheFeed:
    """The four entity types that hang off a feed."""

    @pytest.mark.parametrize("who", ["owner", "member"])
    async def test_trackers(self, session: AsyncSession, world: dict, who: str) -> None:
        result = await session.execute(
            select(Tracker).where(
                Tracker.feed_id.in_(accessible_feed_ids(world[who].id))
            )
        )
        assert [t.id for t in result.scalars().all()] == [world["tracker"].id]

    async def test_trackers_hidden_from_a_stranger(
        self, session: AsyncSession, world: dict
    ) -> None:
        result = await session.execute(
            select(Tracker).where(
                Tracker.feed_id.in_(accessible_feed_ids(world["stranger"].id))
            )
        )
        assert result.scalars().all() == []

    @pytest.mark.parametrize("who", ["owner", "member"])
    async def test_service_alerts(
        self, session: AsyncSession, world: dict, who: str
    ) -> None:
        result = await session.execute(
            select(ServiceAlert).where(
                ServiceAlert.feed_id.in_(accessible_feed_ids(world[who].id))
            )
        )
        assert [a.id for a in result.scalars().all()] == [world["alert"].id]

    async def test_service_alerts_hidden_from_a_stranger(
        self, session: AsyncSession, world: dict
    ) -> None:
        result = await session.execute(
            select(ServiceAlert).where(
                ServiceAlert.feed_id.in_(accessible_feed_ids(world["stranger"].id))
            )
        )
        assert result.scalars().all() == []

    @pytest.mark.parametrize("who", ["owner", "member"])
    async def test_tracker_rules(
        self, session: AsyncSession, world: dict, who: str
    ) -> None:
        result = await session.execute(
            select(TrackerRule)
            .join(Tracker, Tracker.id == TrackerRule.tracker_id)
            .where(Tracker.feed_id.in_(accessible_feed_ids(world[who].id)))
        )
        assert [r.id for r in result.scalars().all()] == [world["rule"].id]

    async def test_tracker_rules_hidden_from_a_stranger(
        self, session: AsyncSession, world: dict
    ) -> None:
        result = await session.execute(
            select(TrackerRule)
            .join(Tracker, Tracker.id == TrackerRule.tracker_id)
            .where(Tracker.feed_id.in_(accessible_feed_ids(world["stranger"].id)))
        )
        assert result.scalars().all() == []

    @pytest.mark.parametrize("who", ["owner", "member"])
    async def test_informed_entities(
        self, session: AsyncSession, world: dict, who: str
    ) -> None:
        result = await session.execute(
            select(InformedEntity)
            .join(ServiceAlert, ServiceAlert.id == InformedEntity.service_alert_id)
            .where(ServiceAlert.feed_id.in_(accessible_feed_ids(world[who].id)))
        )
        assert [e.id for e in result.scalars().all()] == [world["entity"].id]

    async def test_informed_entities_hidden_from_a_stranger(
        self, session: AsyncSession, world: dict
    ) -> None:
        result = await session.execute(
            select(InformedEntity)
            .join(ServiceAlert, ServiceAlert.id == InformedEntity.service_alert_id)
            .where(ServiceAlert.feed_id.in_(accessible_feed_ids(world["stranger"].id)))
        )
        assert result.scalars().all() == []


async def test_a_second_owners_feed_stays_invisible(session: AsyncSession) -> None:
    alice = await make_user(session)
    bob = await make_user(session)
    await make_feed(session, alice, "alice-feed")
    bob_feed = await make_feed(session, bob, "bob-feed")

    assert await _visible_feed_ids(session, bob) == [bob_feed.id]
