"""Adding and removing the people who may work on a feed.

Everything here is called from owner-checked routes; none of it checks
permission itself, with the single exception of the invite-claiming path, which
runs at login and is driven by verified email addresses rather than by a
caller.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple

from railroad_club.models.feed_invite import FeedInvite
from railroad_club.models.feed_member import FeedMember
from railroad_club.models.identity import Identity
from railroad_club.models.user import User
from sqlalchemy import select
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from railroad_club.models.feed import Feed
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)


class ShareResult(NamedTuple):
    """What happened when an owner shared a feed with an address."""

    kind: str  # "member" | "invited" | "already" | "owner"
    message: str


async def list_members(session: AsyncSession, feed_id: int) -> list[FeedMember]:
    result = await session.execute(
        select(FeedMember)
        .where(FeedMember.feed_id == feed_id)
        .options(selectinload(FeedMember.user))
        .order_by(FeedMember.created_at)
    )
    return list(result.scalars().all())


async def list_open_invites(session: AsyncSession, feed_id: int) -> list[FeedInvite]:
    result = await session.execute(
        select(FeedInvite)
        .where(FeedInvite.feed_id == feed_id, FeedInvite.claimed_at.is_(None))
        .order_by(FeedInvite.created_at)
    )
    return list(result.scalars().all())


async def _user_with_verified_email(session: AsyncSession, email: str) -> User | None:
    """The user who has *proven* this address.

    Only verified identities count. An unverified match would let anyone claim
    a share by typing someone else's address into their own provider.
    """
    return await session.scalar(
        select(User)
        .join(Identity, Identity.user_id == User.id)
        .where(Identity.email == email, Identity.email_verified.is_(True))
        .limit(1)
    )


async def share_feed(
    session: AsyncSession, feed: Feed, email: str, *, added_by_user_id: int
) -> ShareResult:
    """Share ``feed`` with ``email``, as a member now or an invite for later."""
    email = email.strip().lower()
    if not email:
        return ShareResult("already", "Enter an email address.")

    user = await _user_with_verified_email(session, email)

    if user is not None and user.id == feed.owner_id:
        return ShareResult("owner", "That is already the owner of this feed.")

    if user is not None:
        existing = await session.scalar(
            select(FeedMember).where(
                FeedMember.feed_id == feed.id, FeedMember.user_id == user.id
            )
        )
        if existing is not None:
            return ShareResult("already", f"{email} already has access.")
        session.add(
            FeedMember(
                feed_id=feed.id, user_id=user.id, added_by_user_id=added_by_user_id
            )
        )
        await session.commit()
        logger.info("share_feed: added member user=%s feed=%s", user.id, feed.id)
        return ShareResult("member", f"{email} now has access.")

    open_invite = await session.scalar(
        select(FeedInvite).where(
            FeedInvite.feed_id == feed.id,
            FeedInvite.email == email,
            FeedInvite.claimed_at.is_(None),
        )
    )
    if open_invite is not None:
        return ShareResult("already", f"{email} is already invited.")

    session.add(
        FeedInvite(feed_id=feed.id, email=email, invited_by_user_id=added_by_user_id)
    )
    await session.commit()
    logger.info("share_feed: invited %s to feed=%s", email, feed.id)
    return ShareResult(
        "invited", f"{email} will get access the next time they sign in."
    )


async def remove_member(session: AsyncSession, feed_id: int, user_id: int) -> None:
    member = await session.scalar(
        select(FeedMember).where(
            FeedMember.feed_id == feed_id, FeedMember.user_id == user_id
        )
    )
    if member is not None:
        await session.delete(member)
        await session.commit()


async def revoke_invite(session: AsyncSession, feed_id: int, invite_id: int) -> None:
    invite = await session.scalar(
        select(FeedInvite).where(
            FeedInvite.id == invite_id,
            FeedInvite.feed_id == feed_id,
            FeedInvite.claimed_at.is_(None),
        )
    )
    if invite is not None:
        await session.delete(invite)
        await session.commit()


async def transfer_ownership(
    session: AsyncSession, feed: Feed, new_owner_id: int
) -> None:
    """Hand a feed to one of its members; the old owner stays on as a member.

    One transaction, because a feed with no owner — or with an owner who is
    also a member — is a state nothing else in the app expects.
    """
    old_owner_id = feed.owner_id
    if new_owner_id == old_owner_id:
        return

    membership = await session.scalar(
        select(FeedMember).where(
            FeedMember.feed_id == feed.id, FeedMember.user_id == new_owner_id
        )
    )
    if membership is None:
        raise PermissionError("Ownership can only be handed to an existing member")

    # The new owner stops being a member: ownership is recorded once, on the
    # feed, so that "is owner" is never ambiguous.
    await session.delete(membership)
    feed.owner_id = new_owner_id
    session.add(
        FeedMember(feed_id=feed.id, user_id=old_owner_id, added_by_user_id=old_owner_id)
    )
    await session.commit()
    logger.info(
        "transfer_ownership: feed=%s %s -> %s", feed.id, old_owner_id, new_owner_id
    )


async def claim_invites(session: AsyncSession, user: User) -> int:
    """Turn this user's outstanding invites into memberships.

    Called at login. Matches only on addresses the user has actually proven,
    and skips a feed they already own or belong to.
    """
    verified = (
        (
            await session.execute(
                select(Identity.email).where(
                    Identity.user_id == user.id,
                    Identity.email_verified.is_(True),
                    Identity.email.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    emails = {e.strip().lower() for e in verified if e}
    if not emails:
        return 0

    invites = (
        (
            await session.execute(
                select(FeedInvite)
                .where(FeedInvite.email.in_(emails), FeedInvite.claimed_at.is_(None))
                .options(selectinload(FeedInvite.feed))
            )
        )
        .scalars()
        .all()
    )
    if not invites:
        return 0

    claimed = 0
    now = datetime.now(UTC)
    for invite in invites:
        invite.claimed_at = now
        invite.claimed_user_id = user.id
        # An invite to a feed they already own or belong to is simply consumed.
        if invite.feed is not None and invite.feed.owner_id == user.id:
            continue
        existing = await session.scalar(
            select(FeedMember).where(
                FeedMember.feed_id == invite.feed_id, FeedMember.user_id == user.id
            )
        )
        if existing is not None:
            continue
        session.add(
            FeedMember(
                feed_id=invite.feed_id,
                user_id=user.id,
                added_by_user_id=invite.invited_by_user_id,
            )
        )
        claimed += 1

    await session.commit()
    if claimed:
        logger.info("claim_invites: user=%s gained %d feeds", user.id, claimed)
    return claimed
