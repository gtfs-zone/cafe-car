"""``/api/me`` and the feed endpoints.

``GET /feeds`` answers "whose feeds are these" through ``personal_feed_ids``,
which does *not* apply the admin bypass: an admin whose feed switcher listed
every feed on the server would never find their own. ``?all=1`` is how they opt
in, and it is refused to everyone else.

``GET /feeds/{id}`` is the opposite case and goes through ``accessible_feed``,
so an admin following a link to someone else's feed still lands on it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from railroad_club.models.feed import Feed
from railroad_club.models.gtfs_static import GtfsStaticFeed
from railroad_club.models.user import User
from sqlalchemy import select

from cafe_car.admin.access import accessible_feed_ids, personal_feed_ids
from cafe_car.api.deps import (
    AccessibleFeed,
    CurrentUser,
    DBSession,
    is_admin,
)
from cafe_car.api.schemas import FeedOut, LoadStatusOut, MeOut
from cafe_car.feed_urls import feed_rt_urls
from cafe_car.settings import get_settings

router = APIRouter()


def _load_status(static: GtfsStaticFeed | None) -> LoadStatusOut | None:
    if static is None:
        return None
    return LoadStatusOut(
        status=static.status,
        error_message=static.error_message,
        timezone=static.timezone,
        last_loaded_at=static.last_loaded_at,
        started_at=static.started_at,
        next_retry_at=static.next_retry_at,
    )


def _feed_out(
    feed: Feed,
    *,
    user_id: int,
    owner: User | None,
    static: GtfsStaticFeed | None,
) -> FeedOut:
    rt = feed_rt_urls(feed)
    return FeedOut(
        id=feed.id,
        feed_name=feed.feed_name,
        static_feed_url=feed.static_feed_url,
        owner_id=feed.owner_id,
        owner_name=(owner.display_name or owner.primary_email) if owner else None,
        is_owner=feed.owner_id == user_id or is_admin(),
        vehicle_positions_url=rt.vehicle_positions,
        trip_updates_url=rt.trip_updates,
        service_alerts_url=rt.service_alerts,
        load=_load_status(static),
    )


@router.get("/me")
async def read_me(user_id: CurrentUser, session: DBSession) -> MeOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return MeOut(
        user_id=user.id,
        email=user.primary_email,
        display_name=user.display_name,
        is_admin=is_admin(),
        account_url=get_settings().keycloak_account_url or None,
    )


@router.get("/feeds")
async def list_feeds(
    user_id: CurrentUser,
    session: DBSession,
    all_feeds: bool = Query(default=False, alias="all"),
) -> list[FeedOut]:
    if all_feeds and not is_admin():
        raise HTTPException(status_code=403, detail="Not an administrator")
    scope = accessible_feed_ids(user_id) if all_feeds else personal_feed_ids(user_id)

    # One statement rather than a walk over `feed.owner` and
    # `feed.gtfs_static_feed`: those are lazy relationships on rows this
    # session will not be holding by the time the serializer runs.
    rows = await session.execute(
        select(Feed, User, GtfsStaticFeed)
        .join(User, User.id == Feed.owner_id)
        .join(
            GtfsStaticFeed,
            GtfsStaticFeed.id == Feed.gtfs_static_feed_id,
            isouter=True,
        )
        .where(Feed.id.in_(scope))
        .order_by(Feed.feed_name)
    )
    return [
        _feed_out(feed, user_id=user_id, owner=owner, static=static)
        for feed, owner, static in rows.all()
    ]


@router.get("/feeds/{feed_id}")
async def read_feed(
    feed: AccessibleFeed, user_id: CurrentUser, session: DBSession
) -> FeedOut:
    owner = await session.get(User, feed.owner_id)
    static = (
        await session.get(GtfsStaticFeed, feed.gtfs_static_feed_id)
        if feed.gtfs_static_feed_id
        else None
    )
    return _feed_out(feed, user_id=user_id, owner=owner, static=static)
