"""Which feeds a user may see.

Every scoped query in the admin goes through here rather than joining to
``user`` itself, so that "who can touch this feed" is defined once. Phase 4
widens :func:`accessible_feed_ids` to include shared feeds; nothing else has to
change when it does.

Phase 8 adds the admin bypass on the same principle. It is applied *here*,
inside the two functions, rather than threaded as an argument through the forty
or so call sites in ``views.py`` and ``entity_router.py``: one place to read
means no call site can be missed, and no caller can pass the wrong flag. The
admin state is read from :data:`current_user_is_admin_var`, which is the same
mechanism ``scaffold_form`` already relies on for the user id.

The security properties this rests on:

* the var defaults to ``False``, so anything that has not explicitly been told
  the caller is an admin gets the ordinary user-scoped query;
* ``SubjectMiddleware`` re-sets it on every request, so a value cannot survive
  from one request into the next;
* it is only ever set ``True`` from a token whose ``sub`` matched the
  oauth2-proxy header (see ``admin/auth.py``), which Traefik's ForwardAuth
  overwrites and a client therefore cannot forge.

This module is imported only by the admin app. The public app on :8000 has no
session, no identity and never calls into here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from railroad_club.models.feed import Feed
from railroad_club.models.feed_member import FeedMember
from sqlalchemy import or_, select

from cafe_car.admin.context import current_user_is_admin_var

if TYPE_CHECKING:
    from sqlalchemy import Select


def _is_admin() -> bool:
    return current_user_is_admin_var.get()


def owned_feed_ids(user_id: int) -> Select[tuple[int]]:
    """Feeds this user owns: the only ones they may delete or hand over.

    An admin owns nothing extra, but is allowed everything an owner is: delete,
    transfer, and managing a feed's members.
    """
    if _is_admin():
        return select(Feed.id)
    return select(Feed.id).where(Feed.owner_id == user_id)


def member_feed_ids(user_id: int) -> Select[tuple[int]]:
    """Feeds shared with this user by someone else.

    Deliberately *not* widened for admins: this answers "who did someone share
    this with", a question of fact, and the members panel would otherwise list
    every admin as a member of every feed.
    """
    return select(FeedMember.feed_id).where(FeedMember.user_id == user_id)


def accessible_feed_ids(user_id: int) -> Select[tuple[int]]:
    """Feeds this user may read and edit: owned or shared with them.

    Trackers, tracker rules, alerts and informed entities all scope through
    their feed, so they inherit sharing from this one definition, and for the
    same reason they inherit the admin bypass.
    """
    if _is_admin():
        return select(Feed.id)
    return select(Feed.id).where(
        or_(Feed.owner_id == user_id, Feed.id.in_(member_feed_ids(user_id)))
    )
