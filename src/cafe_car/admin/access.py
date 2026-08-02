"""Which feeds a user may see.

Every scoped query in the admin goes through here rather than joining to
``user`` itself, so that "who can touch this feed" is defined once. Phase 4
widens :func:`accessible_feed_ids` to include shared feeds; nothing else has to
change when it does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from railroad_club.models.feed import Feed
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy import Select


def owned_feed_ids(user_id: int) -> Select[tuple[int]]:
    """Feeds this user owns — the only ones they may delete or hand over."""
    return select(Feed.id).where(Feed.owner_id == user_id)


def accessible_feed_ids(user_id: int) -> Select[tuple[int]]:
    """Feeds this user may read and edit.

    Currently owner-only. Membership joins in at Phase 4.
    """
    return select(Feed.id).where(Feed.owner_id == user_id)
