"""``/api/me`` and the feed endpoints.

``GET /feeds`` answers "whose feeds are these" through ``personal_feed_ids``,
which does *not* apply the admin bypass: an admin whose feed switcher listed
every feed on the server would never find their own. ``?all=1`` is how they opt
in, and it is refused to everyone else.

``GET /feeds/{id}`` is the opposite case and goes through ``accessible_feed``,
so an admin following a link to someone else's feed still lands on it.

The writes here are deliberately not symmetrical about access. Creating is
open to anyone signed in and the caller becomes the owner. Reloading and
editing only ask for ``accessible_feed``, matching the old admin: re-downloading
the schedule a member is already working against, or fixing the URL it is
downloaded from, is not an owner-only act. Deleting and transferring are, and
go through ``owned_feed``.

Deleting a feed deletes what hangs off it, in one transaction. ``Feed.members``
and ``Feed.invites`` cascade in the model; trackers, rules and alerts do not,
so they are removed here rather than left to raise a foreign-key error. Each
tracker's Traccar device is retired on the way out, best-effort, for the same
reason a deleted tracker's is: a credential nothing answers for is the state
the caller asked for.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from railroad_club.models.feed import Feed
from railroad_club.models.gtfs_static import GtfsStaticFeed
from railroad_club.models.informed_entity import InformedEntity
from railroad_club.models.service_alert import ServiceAlert
from railroad_club.models.tracker import Tracker
from railroad_club.models.tracker_rule import TrackerRule, TrackerRuleException
from railroad_club.models.user import User
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from cafe_car.admin.access import accessible_feed_ids, personal_feed_ids
from cafe_car.api.deps import (
    AccessibleFeed,
    CurrentUser,
    DBSession,
    OwnedFeed,
    is_admin,
)
from cafe_car.api.schemas import (
    FeedCreate,
    FeedOut,
    FeedTransfer,
    FeedUpdate,
    LoadStatusOut,
    MeOut,
)
from cafe_car.feed_load import request_feed_load
from cafe_car.feed_urls import feed_rt_urls
from cafe_car.settings import get_settings
from cafe_car.sharing import transfer_ownership
from cafe_car.traccar import retire_device

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
        is_owner=feed.owner_id == user_id,
        can_manage=feed.owner_id == user_id or is_admin(),
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


@router.post("/feeds", status_code=201)
async def create_feed(
    payload: FeedCreate, user_id: CurrentUser, session: DBSession
) -> FeedOut:
    """Create a feed owned by the caller and queue its first load.

    ``feed_name`` is globally unique and appears in every public GTFS-RT URL,
    so a collision is a 409 the form can show against the field rather than the
    500 an IntegrityError would produce. Checked before the insert *and* caught
    after it: the pre-check is for the message, the catch is for the race.
    """
    feed = Feed(
        feed_name=payload.feed_name,
        static_feed_url=payload.static_feed_url,
        owner_id=user_id,
    )
    taken = await session.scalar(
        select(Feed.id).where(Feed.feed_name == payload.feed_name)
    )
    if taken is not None:
        raise HTTPException(status_code=409, detail="That feed name is taken")

    session.add(feed)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="That feed name is taken") from None
    await session.refresh(feed)

    # There is no `gtfs_static_feed` row yet, so `load` is null until
    # schedule-foamer picks the task up. Every status reader has to handle that.
    request_feed_load(feed.id)

    owner = await session.get(User, user_id)
    return _feed_out(feed, user_id=user_id, owner=owner, static=None)


@router.post("/feeds/{feed_id}/reload", status_code=202)
async def reload_feed(feed: AccessibleFeed) -> Response:
    """Queue a re-download of the feed's static zip.

    202 with no body: the answer is "asked for", not "done". The client learns
    what happened from the feed's load status, which phase 7 pushes down the
    event stream.
    """
    request_feed_load(feed.id)
    return Response(status_code=202)


@router.patch("/feeds/{feed_id}")
async def update_feed(
    payload: FeedUpdate, feed: AccessibleFeed, user_id: CurrentUser, session: DBSession
) -> FeedOut:
    """Rename a feed or repoint its static URL.

    A member may do both, matching the admin this replaces. ``owner_id`` is not
    in :class:`FeedUpdate` at all, so a crafted body cannot move ownership;
    that is what ``/transfer`` is for.

    A rename changes every public GTFS-RT URL this feed serves, which is why
    the name is validated the same way it is on create and why a collision is a
    409 the form can show against the field.
    """
    if payload.feed_name is not None and payload.feed_name != feed.feed_name:
        taken = await session.scalar(
            select(Feed.id).where(
                Feed.feed_name == payload.feed_name, Feed.id != feed.id
            )
        )
        if taken is not None:
            raise HTTPException(status_code=409, detail="That feed name is taken")
        feed.feed_name = payload.feed_name

    # A repointed URL is a different schedule, so the zip is re-downloaded
    # rather than left to `next_retry_at`.
    reload_wanted = (
        payload.static_feed_url is not None
        and payload.static_feed_url != feed.static_feed_url
    )
    if payload.static_feed_url is not None:
        feed.static_feed_url = payload.static_feed_url

    session.add(feed)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="That feed name is taken") from None
    await session.refresh(feed)

    if reload_wanted:
        request_feed_load(feed.id)

    owner = await session.get(User, feed.owner_id)
    static = (
        await session.get(GtfsStaticFeed, feed.gtfs_static_feed_id)
        if feed.gtfs_static_feed_id
        else None
    )
    return _feed_out(feed, user_id=user_id, owner=owner, static=static)


@router.delete("/feeds/{feed_id}", status_code=204)
async def delete_feed(feed: OwnedFeed, session: DBSession) -> Response:
    """Delete a feed and everything that hangs off it.

    Owner-only, and irreversible: the trackers stop resolving, the published
    GTFS-RT URLs stop answering, and the feed name is free for somebody else to
    take. The frontend puts a typed confirmation in front of it.
    """
    tracker_rows = await session.execute(
        select(Tracker.id, Tracker.device_key).where(Tracker.feed_id == feed.id)
    )
    trackers = tracker_rows.all()
    tracker_ids = [t.id for t in trackers]

    alert_ids = (
        (
            await session.execute(
                select(ServiceAlert.id).where(ServiceAlert.feed_id == feed.id)
            )
        )
        .scalars()
        .all()
    )

    if tracker_ids:
        rule_ids = (
            (
                await session.execute(
                    select(TrackerRule.id).where(
                        TrackerRule.tracker_id.in_(tracker_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        if rule_ids:
            await session.execute(
                delete(TrackerRuleException).where(
                    TrackerRuleException.rule_id.in_(rule_ids)
                )
            )
            await session.execute(
                delete(TrackerRule).where(TrackerRule.id.in_(rule_ids))
            )
        await session.execute(delete(Tracker).where(Tracker.id.in_(tracker_ids)))

    if alert_ids:
        await session.execute(
            delete(InformedEntity).where(InformedEntity.service_alert_id.in_(alert_ids))
        )
        await session.execute(
            delete(ServiceAlert).where(ServiceAlert.id.in_(alert_ids))
        )

    await session.delete(feed)
    await session.commit()

    # After the commit: the rows are gone whatever Traccar says, and nothing
    # here may turn a completed delete into an error.
    for tracker in trackers:
        await retire_device(tracker.device_key)

    return Response(status_code=204)


@router.post("/feeds/{feed_id}/transfer")
async def transfer_feed(
    payload: FeedTransfer, feed: OwnedFeed, user_id: CurrentUser, session: DBSession
) -> FeedOut:
    """Hand a feed to one of its members, who must already be one.

    The old owner stays on as a member, so the person doing this does not lose
    their own access by doing it. ``transfer_ownership`` owns both halves in one
    transaction, because a feed with no owner is a state nothing expects.
    """
    try:
        await transfer_ownership(session, feed, payload.new_owner_id)
    except PermissionError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None

    await session.refresh(feed)
    owner = await session.get(User, feed.owner_id)
    static = (
        await session.get(GtfsStaticFeed, feed.gtfs_static_feed_id)
        if feed.gtfs_static_feed_id
        else None
    )
    return _feed_out(feed, user_id=user_id, owner=owner, static=static)
