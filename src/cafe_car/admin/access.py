"""Which feeds a user may see.

Every scoped query in the admin goes through here rather than joining to
``user`` itself, so that "who can touch this feed" is defined once. Phase 4
widens :func:`accessible_feed_ids` to include shared feeds; nothing else has to
change when it does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from railroad_club.models.feed import Feed
from railroad_club.models.feed_member import FeedMember
from sqlalchemy import or_, select

if TYPE_CHECKING:
    from sqlalchemy import Select


def owned_feed_ids(user_id: int) -> Select[tuple[int]]:
    """Feeds this user owns: the only ones they may delete or hand over."""
    return select(Feed.id).where(Feed.owner_id == user_id)


def member_feed_ids(user_id: int) -> Select[tuple[int]]:
    """Feeds shared with this user by someone else."""
    return select(FeedMember.feed_id).where(FeedMember.user_id == user_id)


def accessible_feed_ids(user_id: int) -> Select[tuple[int]]:
    """Feeds this user may read and edit: owned or shared with them.

    Trackers, tracker rules, alerts and informed entities all scope through
    their feed, so they inherit sharing from this one definition.
    """
    return select(Feed.id).where(
        or_(Feed.owner_id == user_id, Feed.id.in_(member_feed_ids(user_id)))
    )
