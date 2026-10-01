"""Who is calling, what they may touch, and the two contracts every route keeps.

Three things live here and nothing else should re-derive them:

* **Identity.** ``resolve_request_user_id`` in ``admin/auth.py`` is the one
  answer to "who is this", shared with ``admin/entity_router.py``. Nothing here
  scopes by the raw proxy header; only by the resolved ``user_id``.
* **Access.** Every feed-scoped route depends on :func:`accessible_feed`, which
  goes through ``admin/access.py::accessible_feed_ids``. A feed the caller
  cannot see answers **404, not 403**, so the endpoint does not confirm that an
  id exists.
* **CSRF.** The session is a cookie, so a cross-site form post would otherwise
  be authenticated. :func:`require_csrf` is mounted on the whole ``/api``
  router, so every mutation added in a later phase inherits it without a route
  having to remember. A simple cross-site request cannot set a custom header,
  and a preflighted one is blocked by the absence of CORS.

The session-expiry contract is the fourth: oauth2-proxy answers an expired
session with a 302 to Keycloak before a request ever reaches this app, so a
client sees a redirect chain ending in HTML rather than any response built
here. What this module owes the client is that every answer it *does* build is
JSON, including its errors, so "not JSON" is an unambiguous signal to reload.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from gtfs_zone_db_models.models.feed import Feed
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gtfs_zone_rt_api.admin.access import accessible_feed_ids, owned_feed_ids
from gtfs_zone_rt_api.admin.auth import ensure_identity, resolve_request_user_id
from gtfs_zone_rt_api.admin.context import current_user_is_admin_var

# The header rt-manager sends on every mutation. Named for the app rather than
# something generic so it cannot be confused with a proxy or framework header.
CSRF_HEADER = "X-RT-Manager"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def require_csrf(request: Request) -> None:
    """Reject any unsafe method that did not come from our own frontend."""
    if request.method in SAFE_METHODS:
        return
    if not request.headers.get(CSRF_HEADER):
        raise HTTPException(status_code=403, detail=f"Missing {CSRF_HEADER} header")


def db_session(request: Request) -> AsyncSession:
    """The request's session, opened by ``DBSessionMiddleware``.

    Deliberately not ``database.get_session``: that would open a second session
    per request, so a route and the middleware could read two different
    snapshots of the same rows.
    """
    return request.state.session


DBSession = Annotated[AsyncSession, Depends(db_session)]


async def current_user_id(request: Request, session: DBSession) -> int:
    """The signed-in user, or 401.

    Reaching here without a subject header means oauth2-proxy is not in front
    of the app; that alone is a 401, not recoverable by retrying. A subject
    header with no existing Identity row is a first-sight credential, not an
    error: ``ensure_identity`` creates the row and claims any invites waiting
    on it, the same provisioning SQLAdmin's auth backend used to do.
    """
    user_id = await resolve_request_user_id(request, session)
    if not user_id:
        user_id = await ensure_identity(request, session)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user_id


CurrentUser = Annotated[int, Depends(current_user_id)]


def is_admin() -> bool:
    """Whether this caller is in the admin group.

    Read rather than recomputed: ``resolve_request_user_id`` has already set it
    from the token, and ``CurrentUser`` runs first on every route that asks.
    """
    return current_user_is_admin_var.get()


async def accessible_feed(
    feed_id: int, user_id: CurrentUser, session: DBSession
) -> Feed:
    """A feed the caller owns or has been given, or 404."""
    feed = await session.scalar(
        select(Feed).where(
            Feed.id.in_(accessible_feed_ids(user_id)), Feed.id == feed_id
        )
    )
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    return feed


AccessibleFeed = Annotated[Feed, Depends(accessible_feed)]


async def owned_feed(feed_id: int, user_id: CurrentUser, session: DBSession) -> Feed:
    """A feed the caller may delete, transfer or manage the members of.

    Goes through ``owned_feed_ids`` so the admin bypass keeps its one
    definition. A feed the caller can see but does not own is 403 here, not
    404: they already know it exists.
    """
    feed = await session.scalar(
        select(Feed).where(Feed.id.in_(owned_feed_ids(user_id)), Feed.id == feed_id)
    )
    if feed is not None:
        return feed
    # Distinguish the two failures only once the caller has proven they can see
    # the feed at all: a stranger still gets 404, so no id is confirmed.
    visible = await session.scalar(
        select(Feed.id).where(
            Feed.id.in_(accessible_feed_ids(user_id)), Feed.id == feed_id
        )
    )
    if visible is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    raise HTTPException(status_code=403, detail="Only the owner can do that")


OwnedFeed = Annotated[Feed, Depends(owned_feed)]
