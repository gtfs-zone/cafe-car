"""Small builders for the rows most tests need.

Deliberately thin: they exist so a test can say "a user who has proven this
address" in one line, not to hide what is being set up.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from railroad_club.models.feed import Feed
from railroad_club.models.feed_member import FeedMember
from railroad_club.models.identity import Identity
from railroad_club.models.user import User

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

_counter = itertools.count(1)

PROVIDER = "keycloak"


async def make_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    verified: bool = True,
    display_name: str | None = None,
    provider: str = PROVIDER,
    subject: str | None = None,
) -> User:
    """A person with one identity, which is how everybody arrives."""
    n = next(_counter)
    email = email if email is not None else f"user{n}@example.com"
    user = User(primary_email=email, display_name=display_name or f"User {n}")
    session.add(user)
    await session.flush()
    session.add(
        Identity(
            user_id=user.id,
            provider=provider,
            provider_subject=subject or f"subject-{n}",
            email=email,
            email_verified=verified,
        )
    )
    await session.commit()
    return user


async def add_identity(
    session: AsyncSession,
    user: User,
    *,
    email: str | None = None,
    verified: bool = True,
    provider: str = PROVIDER,
    subject: str | None = None,
) -> Identity:
    """A second way to sign in as an existing person."""
    n = next(_counter)
    identity = Identity(
        user_id=user.id,
        provider=provider,
        provider_subject=subject or f"subject-{n}",
        email=email if email is not None else f"user{n}@example.com",
        email_verified=verified,
    )
    session.add(identity)
    await session.commit()
    return identity


async def make_feed(
    session: AsyncSession, owner: User, name: str | None = None
) -> Feed:
    # feed_name is validated against ^[a-z][a-z0-9_-]{2,63}$, so it cannot just
    # be a bare counter.
    feed = Feed(
        feed_name=name or f"feed-{next(_counter)}",
        static_feed_url="https://example.com/gtfs.zip",
        owner_id=owner.id,
    )
    session.add(feed)
    await session.commit()
    return feed


async def add_member(
    session: AsyncSession, feed: Feed, user: User, *, added_by: User | None = None
) -> FeedMember:
    member = FeedMember(
        feed_id=feed.id,
        user_id=user.id,
        added_by_user_id=(added_by or user).id,
    )
    session.add(member)
    await session.commit()
    return member
