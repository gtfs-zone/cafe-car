"""Resolving a login to a person.

An OIDC login arrives as ``(provider, subject)``. That pair identifies an
:class:`Identity`, and the identity points at the :class:`User` who actually
owns things. A person with GitHub and Google linked has two identities and one
user — which is the entire point of the split.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from railroad_club.models.feed import Feed
from railroad_club.models.feed_invite import FeedInvite
from railroad_club.models.feed_member import FeedMember
from railroad_club.models.identity import Identity
from railroad_club.models.user import User
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)


async def resolve_login(
    session: AsyncSession,
    *,
    provider: str,
    subject: str,
    email: str | None = None,
    email_verified: bool = False,
    display_name: str | None = None,
) -> User:
    """Return the :class:`User` for this login, creating one if needed.

    Refreshes the profile fields we were told about, so a changed display name
    or a newly verified email lands in the database on the next sign-in.
    """
    identity = await session.scalar(
        select(Identity)
        .where(Identity.provider == provider, Identity.provider_subject == subject)
        .options(selectinload(Identity.user))
    )

    if identity is not None:
        user = identity.user
        _refresh_profile(
            identity,
            user,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
        )
        await session.commit()
        return user

    # An unseen credential. Phase 6 inserts the "a verified email already
    # belongs to someone — link instead?" check here; until then a new
    # credential always means a new person.
    logger.info("resolve_login: new identity provider=%s subject=%s", provider, subject)
    user = User(primary_email=email, display_name=display_name)
    session.add(user)
    await session.flush()
    session.add(
        Identity(
            user_id=user.id,
            provider=provider,
            provider_subject=subject,
            email=email,
            email_verified=email_verified,
        )
    )
    await session.commit()
    return user


def choose_absorber(a: User, b: User) -> tuple[User, User]:
    """Order two users as ``(absorbing, absorbed)``.

    The older account absorbs. Feeds, memberships and invites all reference a
    ``user.id``, and the older id has had longer to be referenced — from
    outside the database too, in bookmarks and logs — so it is the one worth
    keeping.
    """
    if a.created_at != b.created_at:
        return (a, b) if a.created_at < b.created_at else (b, a)
    return (a, b) if (a.id or 0) < (b.id or 0) else (b, a)


async def merge_users(
    session: AsyncSession, *, absorbing_id: int, absorbed_id: int
) -> None:
    """Fold ``absorbed_id`` into ``absorbing_id`` and delete the absorbed user.

    One transaction. A half-merged pair — feeds moved but identities not, say —
    would leave someone locked out of their own data with no way to tell from
    the outside, so there is no partial success here.

    Callers are responsible for having established that the two accounts really
    are one person; this function does no authorisation of its own.
    """
    if absorbing_id == absorbed_id:
        raise ValueError("Cannot merge a user into itself")

    absorbing = await session.get(User, absorbing_id)
    absorbed = await session.get(User, absorbed_id)
    if absorbing is None or absorbed is None:
        missing = absorbing_id if absorbing is None else absorbed_id
        raise LookupError(f"No such user: {missing}")

    # 1. Feeds. Do this first: it changes which feeds the absorbing user owns,
    #    which is what the membership pass below has to reconcile against.
    await session.execute(
        update(Feed).where(Feed.owner_id == absorbed_id).values(owner_id=absorbing_id)
    )

    # 2. Memberships. Two ways a row cannot simply be reassigned: the absorbing
    #    user is already a member of that feed (UNIQUE(feed_id, user_id)), or
    #    they now own it — and an owner is never also a member of their own
    #    feed. Both resolve by dropping the redundant row, not by moving it.
    owned_now = set(
        (await session.execute(select(Feed.id).where(Feed.owner_id == absorbing_id)))
        .scalars()
        .all()
    )
    rows = (
        (
            await session.execute(
                select(FeedMember).where(
                    FeedMember.user_id.in_((absorbing_id, absorbed_id))
                )
            )
        )
        .scalars()
        .all()
    )
    kept_feed_ids = {m.feed_id for m in rows if m.user_id == absorbing_id}
    for member in rows:
        if member.feed_id in owned_now:
            await session.delete(member)
        elif member.user_id == absorbed_id:
            if member.feed_id in kept_feed_ids:
                await session.delete(member)
            else:
                member.user_id = absorbing_id
                kept_feed_ids.add(member.feed_id)

    # 3. Audit trails. Nullable and non-unique, so a straight rewrite.
    await session.execute(
        update(FeedMember)
        .where(FeedMember.added_by_user_id == absorbed_id)
        .values(added_by_user_id=absorbing_id)
    )
    await session.execute(
        update(FeedInvite)
        .where(FeedInvite.invited_by_user_id == absorbed_id)
        .values(invited_by_user_id=absorbing_id)
    )
    await session.execute(
        update(FeedInvite)
        .where(FeedInvite.claimed_user_id == absorbed_id)
        .values(claimed_user_id=absorbing_id)
    )

    # 4. The credentials themselves — the point of the exercise.
    await session.execute(
        update(Identity)
        .where(Identity.user_id == absorbed_id)
        .values(user_id=absorbing_id)
    )

    # 5. Fill in anything the surviving profile is missing, without clobbering
    #    what it already says.
    if not absorbing.primary_email:
        absorbing.primary_email = absorbed.primary_email
    if not absorbing.display_name:
        absorbing.display_name = absorbed.display_name

    await session.delete(absorbed)
    await session.commit()
    logger.info("merge_users: %s absorbed into %s", absorbed_id, absorbing_id)


def _refresh_profile(
    identity: Identity,
    user: User,
    *,
    email: str | None,
    email_verified: bool,
    display_name: str | None,
) -> None:
    if email and identity.email != email:
        identity.email = email
        # A changed address is unverified until this login says otherwise.
        identity.email_verified = email_verified
    elif email and email_verified and not identity.email_verified:
        identity.email_verified = True
    if email and user.primary_email != email:
        user.primary_email = email
    if display_name and user.display_name != display_name:
        user.display_name = display_name
