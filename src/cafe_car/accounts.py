"""Resolving a login to a person.

An OIDC login arrives as ``(provider, subject)``. That pair identifies an
:class:`Identity`, and the identity points at the :class:`User` who actually
owns things. A person with GitHub and Google linked has two identities and one
user, which is the entire point of the split.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from railroad_club.models.feed import Feed
from railroad_club.models.feed_invite import FeedInvite
from railroad_club.models.feed_member import FeedMember
from railroad_club.models.identity import Identity
from railroad_club.models.user import User
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)

# How stale ``Identity.last_seen_at`` is allowed to get before a login rewrites
# it. The column exists to tell a live credential from a dormant one, and no
# question that answers needs the resolution finer than this.
_LAST_SEEN_GRAIN = timedelta(minutes=15)


class LoginResult(NamedTuple):
    """Who signed in, and whether they look like someone we already know.

    ``link_candidate_id`` is set only when this login was a brand-new
    credential whose *verified* address already belongs to another user. It is
    a suggestion to show them, never an action: merging happens on their
    explicit confirmation and nowhere else.
    """

    user: User
    link_candidate_id: int | None = None


async def user_with_verified_email(
    session: AsyncSession, email: str, *, exclude_user_id: int | None = None
) -> User | None:
    """The user who has *proven* this address.

    Only verified identities count. Matching an unverified address would let
    anyone claim someone else's account by typing their email into a provider
    that does not check.

    Compared case-insensitively on both sides: unlike ``FeedInvite.email``,
    ``Identity.email`` is stored exactly as the provider sent it, so the stored
    value cannot be assumed lowercase. That gives up the index on a table with
    one row per linked credential, which is not a table that grows.
    """
    query = (
        select(User)
        .join(Identity, Identity.user_id == User.id)
        .where(func.lower(Identity.email) == email.strip().lower())
        .where(Identity.email_verified.is_(True))
    )
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    return await session.scalar(query.limit(1))


async def resolve_login(
    session: AsyncSession,
    *,
    provider: str,
    subject: str,
    email: str | None = None,
    email_verified: bool = False,
    display_name: str | None = None,
    broker_alias: str | None = None,
) -> LoginResult:
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
            broker_alias=broker_alias,
        )
        await session.commit()
        return LoginResult(user)

    # An unseen credential. Keycloak's first-broker-login flow normally catches
    # the "this email already has an account" case upstream and links there, so
    # reaching here with a known address means the same person exists in
    # Keycloak twice. Create the new principal regardless, since a silent merge
    # on an email match is exactly the takeover primitive this whole design
    # avoids, and hand the caller a candidate to offer them.
    candidate = None
    if email and email_verified:
        existing = await user_with_verified_email(session, email)
        if existing is not None:
            candidate = existing.id
            logger.info(
                "resolve_login: verified email %s already belongs to user=%s",
                email,
                candidate,
            )

    logger.info("resolve_login: new identity provider=%s subject=%s", provider, subject)
    if email and not email_verified:
        # Worth a warning, not an info: this credential cannot claim a feed
        # invite, cannot be shared with by address, and cannot be offered as a
        # link candidate, all of which look like the feature is broken rather
        # than like the issuer never vouched for the address. A broker with
        # `trustEmail` off is the usual cause.
        logger.warning(
            "resolve_login: new identity subject=%s has unverified email %s, "
            "invites and account linking will not match it",
            subject,
            email,
        )
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
            broker_alias=broker_alias,
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.commit()
    return LoginResult(user, candidate)


async def link_candidates(session: AsyncSession, user_id: int) -> list[User]:
    """Other users who have proven one of this user's verified addresses.

    Recomputed on every view rather than remembered. ``authenticate`` clears
    the session on each request, so a stashed suggestion would not survive to
    be acted on; and a live query is self-healing: once the accounts are
    merged, or the address stops being verified, the offer simply stops
    appearing.
    """
    emails = (
        (
            await session.execute(
                select(Identity.email).where(
                    Identity.user_id == user_id,
                    Identity.email_verified.is_(True),
                    Identity.email.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    found: dict[int, User] = {}
    for email in {e.strip().lower() for e in emails if e}:
        other = await user_with_verified_email(session, email, exclude_user_id=user_id)
        if other is not None and other.id not in found:
            found[other.id] = other
    return list(found.values())


def choose_absorber(a: User, b: User) -> tuple[User, User]:
    """Order two users as ``(absorbing, absorbed)``.

    The older account absorbs. Feeds, memberships and invites all reference a
    ``user.id``, and the older id has had longer to be referenced, from
    outside the database too, in bookmarks and logs, so it is the one worth
    keeping.
    """
    if a.created_at != b.created_at:
        return (a, b) if a.created_at < b.created_at else (b, a)
    return (a, b) if (a.id or 0) < (b.id or 0) else (b, a)


async def merge_users(
    session: AsyncSession, *, absorbing_id: int, absorbed_id: int
) -> None:
    """Fold ``absorbed_id`` into ``absorbing_id`` and delete the absorbed user.

    One transaction. A half-merged pair (feeds moved but identities not, say)
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
    #    they now own it, and an owner is never also a member of their own
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

    # 4. The credentials themselves, the point of the exercise.
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
    broker_alias: str | None = None,
) -> None:
    # The claim is absent whenever the issuer did not bother to send it, and
    # "we were not told this time" is not evidence that what we were told
    # before was wrong, so an absent claim never clobbers a known value. But
    # a *present* claim is current, positive information (this login really
    # did come through this broker) and must overwrite whatever broker was
    # recorded on a previous login of the same account.
    if broker_alias:
        identity.broker_alias = broker_alias
    now = datetime.now(UTC)
    seen = identity.last_seen_at
    # SQLite hands back a naive datetime for a timezone-aware column, so the
    # subtraction below would raise there rather than in production.
    if seen is not None and seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    if seen is None or now - seen > _LAST_SEEN_GRAIN:
        # `authenticate` runs on every request; without the grain this would
        # turn every page view into a write.
        identity.last_seen_at = now
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
