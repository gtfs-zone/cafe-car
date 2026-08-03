"""Adding, removing and inviting the people who may work on a feed."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from railroad_club.models.feed_invite import FeedInvite
from railroad_club.models.feed_member import FeedMember
from sqlalchemy import select

from cafe_car.sharing import (
    claim_invites,
    list_members,
    list_open_invites,
    remove_member,
    revoke_invite,
    share_feed,
    transfer_ownership,
)
from tests.factories import add_identity, add_member, make_feed, make_user

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession


class TestShareFeed:
    async def test_a_registered_verified_address_becomes_a_member(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        friend = await make_user(session, email="friend@example.com")
        feed = await make_feed(session, owner)

        result = await share_feed(
            session, feed, "friend@example.com", added_by_user_id=owner.id
        )

        assert result.kind == "member"
        assert [m.user_id for m in await list_members(session, feed.id)] == [friend.id]

    async def test_matching_is_case_insensitive(self, session: AsyncSession) -> None:
        owner = await make_user(session, email="owner@example.com")
        friend = await make_user(session, email="Friend@Example.com")
        feed = await make_feed(session, owner)

        result = await share_feed(
            session, feed, "  FRIEND@example.COM  ", added_by_user_id=owner.id
        )

        assert result.kind == "member"
        assert [m.user_id for m in await list_members(session, feed.id)] == [friend.id]

    async def test_an_unknown_address_becomes_an_invite(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)

        result = await share_feed(
            session, feed, "Nobody@Example.com", added_by_user_id=owner.id
        )

        assert result.kind == "invited"
        invites = await list_open_invites(session, feed.id)
        assert [i.email for i in invites] == ["nobody@example.com"]

    async def test_an_unverified_address_becomes_an_invite_not_a_member(
        self, session: AsyncSession
    ) -> None:
        """An unverified match would let anyone take a share by typing someone
        else's address into a provider that does not check."""
        owner = await make_user(session, email="owner@example.com")
        await make_user(session, email="unproven@example.com", verified=False)
        feed = await make_feed(session, owner)

        result = await share_feed(
            session, feed, "unproven@example.com", added_by_user_id=owner.id
        )

        assert result.kind == "invited"
        assert await list_members(session, feed.id) == []

    async def test_sharing_with_the_owner_is_refused(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)

        result = await share_feed(
            session, feed, "owner@example.com", added_by_user_id=owner.id
        )

        assert result.kind == "owner"
        assert await list_members(session, feed.id) == []

    async def test_sharing_twice_is_idempotent(self, session: AsyncSession) -> None:
        owner = await make_user(session, email="owner@example.com")
        await make_user(session, email="friend@example.com")
        feed = await make_feed(session, owner)

        await share_feed(session, feed, "friend@example.com", added_by_user_id=owner.id)
        again = await share_feed(
            session, feed, "friend@example.com", added_by_user_id=owner.id
        )

        assert again.kind == "already"
        assert len(await list_members(session, feed.id)) == 1

    async def test_inviting_twice_is_idempotent(self, session: AsyncSession) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)

        await share_feed(session, feed, "nobody@example.com", added_by_user_id=owner.id)
        again = await share_feed(
            session, feed, "nobody@example.com", added_by_user_id=owner.id
        )

        assert again.kind == "already"
        assert len(await list_open_invites(session, feed.id)) == 1

    async def test_an_empty_address_does_nothing(self, session: AsyncSession) -> None:
        owner = await make_user(session)
        feed = await make_feed(session, owner)

        result = await share_feed(session, feed, "   ", added_by_user_id=owner.id)

        assert result.kind == "already"
        assert await list_open_invites(session, feed.id) == []


class TestRemoval:
    async def test_remove_member(self, session: AsyncSession) -> None:
        owner = await make_user(session)
        friend = await make_user(session)
        feed = await make_feed(session, owner)
        await add_member(session, feed, friend, added_by=owner)

        await remove_member(session, feed.id, friend.id)

        assert await list_members(session, feed.id) == []

    async def test_removing_a_non_member_is_harmless(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session)
        stranger = await make_user(session)
        feed = await make_feed(session, owner)

        await remove_member(session, feed.id, stranger.id)

        assert await list_members(session, feed.id) == []

    async def test_revoke_invite(self, session: AsyncSession) -> None:
        owner = await make_user(session)
        feed = await make_feed(session, owner)
        await share_feed(session, feed, "nobody@example.com", added_by_user_id=owner.id)
        invite = (await list_open_invites(session, feed.id))[0]

        await revoke_invite(session, feed.id, invite.id)

        assert await list_open_invites(session, feed.id) == []

    async def test_an_invite_cannot_be_revoked_through_another_feed(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session)
        feed = await make_feed(session, owner, "one-feed")
        other = await make_feed(session, owner, "other-feed")
        await share_feed(session, feed, "nobody@example.com", added_by_user_id=owner.id)
        invite = (await list_open_invites(session, feed.id))[0]

        await revoke_invite(session, other.id, invite.id)

        assert len(await list_open_invites(session, feed.id)) == 1


class TestTransferOwnership:
    async def test_the_old_owner_stays_on_as_a_member(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session)
        friend = await make_user(session)
        feed = await make_feed(session, owner)
        await add_member(session, feed, friend, added_by=owner)

        await transfer_ownership(session, feed, friend.id)

        await session.refresh(feed)
        assert feed.owner_id == friend.id
        assert [m.user_id for m in await list_members(session, feed.id)] == [owner.id]

    async def test_a_non_member_cannot_be_handed_the_feed(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session)
        stranger = await make_user(session)
        feed = await make_feed(session, owner)

        with pytest.raises(PermissionError):
            await transfer_ownership(session, feed, stranger.id)

        await session.refresh(feed)
        assert feed.owner_id == owner.id

    async def test_transferring_to_the_current_owner_is_a_no_op(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session)
        feed = await make_feed(session, owner)

        await transfer_ownership(session, feed, owner.id)

        await session.refresh(feed)
        assert feed.owner_id == owner.id
        assert await list_members(session, feed.id) == []


class TestClaimInvites:
    async def test_a_verified_address_claims_a_pending_invite(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)
        await share_feed(session, feed, "later@example.com", added_by_user_id=owner.id)
        newcomer = await make_user(session, email="later@example.com", verified=True)

        assert await claim_invites(session, newcomer) == 1

        assert [m.user_id for m in await list_members(session, feed.id)] == [
            newcomer.id
        ]
        assert await list_open_invites(session, feed.id) == []

    async def test_matching_is_case_insensitive(self, session: AsyncSession) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)
        await share_feed(session, feed, "Later@Example.com", added_by_user_id=owner.id)
        newcomer = await make_user(session, email="LATER@EXAMPLE.COM", verified=True)

        assert await claim_invites(session, newcomer) == 1

    async def test_an_unverified_address_claims_nothing(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)
        await share_feed(session, feed, "later@example.com", added_by_user_id=owner.id)
        newcomer = await make_user(session, email="later@example.com", verified=False)

        assert await claim_invites(session, newcomer) == 0
        assert await list_members(session, feed.id) == []
        assert len(await list_open_invites(session, feed.id)) == 1

    async def test_a_second_verified_identity_can_claim(
        self, session: AsyncSession
    ) -> None:
        """Invites match on any address the person has proven, not just the
        primary one."""
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)
        await share_feed(session, feed, "work@example.com", added_by_user_id=owner.id)
        newcomer = await make_user(session, email="home@example.com")
        await add_identity(session, newcomer, email="work@example.com", verified=True)

        assert await claim_invites(session, newcomer) == 1

    async def test_claiming_is_idempotent(self, session: AsyncSession) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)
        await share_feed(session, feed, "later@example.com", added_by_user_id=owner.id)
        newcomer = await make_user(session, email="later@example.com")

        assert await claim_invites(session, newcomer) == 1
        assert await claim_invites(session, newcomer) == 0
        assert len(await list_members(session, feed.id)) == 1

    async def test_an_invite_to_a_feed_they_already_own_is_consumed_not_applied(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)
        # Sneak the invite in directly: share_feed would refuse the owner.
        session.add(
            FeedInvite(
                feed_id=feed.id,
                email="owner@example.com",
                invited_by_user_id=owner.id,
            )
        )
        await session.commit()

        assert await claim_invites(session, owner) == 0
        assert await list_open_invites(session, feed.id) == []
        assert await list_members(session, feed.id) == []

    async def test_an_invite_to_a_feed_they_already_belong_to_is_consumed(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        friend = await make_user(session, email="friend@example.com")
        feed = await make_feed(session, owner)
        await add_member(session, feed, friend, added_by=owner)
        session.add(
            FeedInvite(
                feed_id=feed.id,
                email="friend@example.com",
                invited_by_user_id=owner.id,
            )
        )
        await session.commit()

        assert await claim_invites(session, friend) == 0
        assert await list_open_invites(session, feed.id) == []
        assert len(await list_members(session, feed.id)) == 1

    async def test_a_claimed_invite_records_who_took_it(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)
        await share_feed(session, feed, "later@example.com", added_by_user_id=owner.id)
        newcomer = await make_user(session, email="later@example.com")

        await claim_invites(session, newcomer)

        invite = await session.scalar(select(FeedInvite))
        assert invite.claimed_user_id == newcomer.id
        assert invite.claimed_at is not None

    async def test_the_membership_credits_the_original_inviter(
        self, session: AsyncSession
    ) -> None:
        owner = await make_user(session, email="owner@example.com")
        feed = await make_feed(session, owner)
        await share_feed(session, feed, "later@example.com", added_by_user_id=owner.id)
        newcomer = await make_user(session, email="later@example.com")

        await claim_invites(session, newcomer)

        member = await session.scalar(select(FeedMember))
        assert member.added_by_user_id == owner.id
