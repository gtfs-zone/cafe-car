"""Merging two principals into one.

The sharp edge of account linking: feeds, memberships and invites all have to
move across, through a ``UNIQUE(feed_id, user_id)`` constraint and the Phase 4
invariant that an owner is never also a member of their own feed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from railroad_club.models.feed import Feed
from railroad_club.models.feed_invite import FeedInvite
from railroad_club.models.feed_member import FeedMember
from railroad_club.models.identity import Identity
from railroad_club.models.user import User
from sqlalchemy import select

from cafe_car.accounts import choose_absorber, merge_users
from tests.factories import add_identity, add_member, make_feed, make_user

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession


async def _members(session: AsyncSession, feed: Feed) -> list[FeedMember]:
    result = await session.execute(
        select(FeedMember).where(FeedMember.feed_id == feed.id)
    )
    return list(result.scalars().all())


async def test_absorbed_user_is_gone_and_identities_move(session: AsyncSession) -> None:
    keeper = await make_user(session, email="a@example.com")
    doomed = await make_user(session, email="b@example.com")
    await add_identity(session, doomed, email="b@example.com", subject="second")

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    assert await session.get(User, doomed.id) is None
    identities = (
        (await session.execute(select(Identity).where(Identity.user_id == keeper.id)))
        .scalars()
        .all()
    )
    assert len(identities) == 3
    assert all(i.user_id == keeper.id for i in identities)


async def test_feeds_change_owner_but_not_id(session: AsyncSession) -> None:
    keeper = await make_user(session)
    doomed = await make_user(session)
    kept_feed = await make_feed(session, keeper, "keeper-feed")
    moved_feed = await make_feed(session, doomed, "doomed-feed")

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    for feed in (kept_feed, moved_feed):
        refreshed = await session.get(Feed, feed.id)
        assert refreshed is not None, "feed.id must survive the merge"
        assert refreshed.owner_id == keeper.id


async def test_membership_moves_when_there_is_no_conflict(
    session: AsyncSession,
) -> None:
    keeper = await make_user(session)
    doomed = await make_user(session)
    stranger = await make_user(session)
    feed = await make_feed(session, stranger)
    await add_member(session, feed, doomed, added_by=stranger)

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    members = await _members(session, feed)
    assert [m.user_id for m in members] == [keeper.id]


async def test_duplicate_membership_is_dropped_not_moved(
    session: AsyncSession,
) -> None:
    """Both were members of the same feed: UNIQUE(feed_id, user_id) forbids two."""
    keeper = await make_user(session)
    doomed = await make_user(session)
    stranger = await make_user(session)
    feed = await make_feed(session, stranger)
    await add_member(session, feed, keeper, added_by=stranger)
    await add_member(session, feed, doomed, added_by=stranger)

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    members = await _members(session, feed)
    assert [m.user_id for m in members] == [keeper.id]


async def test_absorbed_membership_of_a_feed_the_keeper_owns_is_dropped(
    session: AsyncSession,
) -> None:
    keeper = await make_user(session)
    doomed = await make_user(session)
    feed = await make_feed(session, keeper)
    await add_member(session, feed, doomed, added_by=keeper)

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    assert await _members(session, feed) == []
    refreshed = await session.get(Feed, feed.id)
    assert refreshed.owner_id == keeper.id


async def test_keeper_membership_of_a_feed_the_absorbed_owns_is_dropped(
    session: AsyncSession,
) -> None:
    """The subtle one: reassigning the feed makes the keeper its owner, so the
    membership row they already held would leave an owner who is also a
    member."""
    keeper = await make_user(session)
    doomed = await make_user(session)
    feed = await make_feed(session, doomed)
    await add_member(session, feed, keeper, added_by=doomed)

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    refreshed = await session.get(Feed, feed.id)
    assert refreshed.owner_id == keeper.id
    assert await _members(session, feed) == []


async def test_audit_columns_are_reassigned(session: AsyncSession) -> None:
    keeper = await make_user(session)
    doomed = await make_user(session)
    other = await make_user(session)
    feed = await make_feed(session, doomed)
    member = await add_member(session, feed, other, added_by=doomed)
    invite = FeedInvite(
        feed_id=feed.id, email="later@example.com", invited_by_user_id=doomed.id
    )
    claimed = FeedInvite(
        feed_id=feed.id,
        email="earlier@example.com",
        invited_by_user_id=other.id,
        claimed_at=datetime.now(UTC),
        claimed_user_id=doomed.id,
    )
    session.add_all([invite, claimed])
    await session.commit()

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    await session.refresh(member)
    await session.refresh(invite)
    await session.refresh(claimed)
    assert member.added_by_user_id == keeper.id
    assert invite.invited_by_user_id == keeper.id
    assert claimed.claimed_user_id == keeper.id


async def test_profile_fields_are_backfilled_but_not_overwritten(
    session: AsyncSession,
) -> None:
    keeper = User(primary_email=None, display_name=None)
    session.add(keeper)
    await session.commit()
    doomed = await make_user(
        session, email="them@example.com", display_name="Their Name"
    )

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    await session.refresh(keeper)
    assert keeper.primary_email == "them@example.com"
    assert keeper.display_name == "Their Name"


async def test_named_keeper_keeps_its_own_profile(session: AsyncSession) -> None:
    keeper = await make_user(session, email="mine@example.com", display_name="Mine")
    doomed = await make_user(session, email="theirs@example.com", display_name="Theirs")

    await merge_users(session, absorbing_id=keeper.id, absorbed_id=doomed.id)

    await session.refresh(keeper)
    assert keeper.primary_email == "mine@example.com"
    assert keeper.display_name == "Mine"


async def test_merging_a_user_into_itself_is_refused(session: AsyncSession) -> None:
    user = await make_user(session)
    with pytest.raises(ValueError):
        await merge_users(session, absorbing_id=user.id, absorbed_id=user.id)


async def test_merging_a_missing_user_is_refused(session: AsyncSession) -> None:
    user = await make_user(session)
    with pytest.raises(LookupError):
        await merge_users(session, absorbing_id=user.id, absorbed_id=user.id + 999)


class TestChooseAbsorber:
    """The older account absorbs, so the longer-lived user.id that feeds
    reference is the one that survives."""

    def test_older_created_at_wins(self) -> None:
        now = datetime.now(UTC)
        older = User(id=7, created_at=now - timedelta(days=30))
        newer = User(id=2, created_at=now)
        assert choose_absorber(newer, older) == (older, newer)
        assert choose_absorber(older, newer) == (older, newer)

    def test_equal_timestamps_fall_back_to_the_lower_id(self) -> None:
        now = datetime.now(UTC)
        first = User(id=3, created_at=now)
        second = User(id=9, created_at=now)
        assert choose_absorber(second, first) == (first, second)
