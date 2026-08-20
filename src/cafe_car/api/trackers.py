"""Tracker endpoints, and the one place the provisioning credential is served.

A tracker's ``id`` is its Traccar ``uniqueId`` and there is no password behind
it, so it is the whole secret. The list endpoint returns :class:`TrackerOut`,
which has no ``id`` at all; the detail endpoints return
:class:`TrackerDetailOut`, which does.

That leaves the client needing a way to reach a tracker it has only ever seen
by nickname, which is the only name it is allowed to keep in navigation state.
Hence two detail routes for the one object: ``/feeds/{id}/trackers/{nickname}``
is what the panel actually navigates through, and ``/trackers/{id}`` is the
resource path for a caller that already holds the credential.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from railroad_club.models.tracker import Tracker
from sqlalchemy import select

from cafe_car.admin.access import accessible_feed_ids
from cafe_car.api.deps import AccessibleFeed, CurrentUser, DBSession
from cafe_car.api.schemas import TrackerDetailOut, TrackerOut

router = APIRouter()


def _detail(tracker: Tracker) -> TrackerDetailOut:
    return TrackerDetailOut(
        id=tracker.id, nickname=tracker.nickname, feed_id=tracker.feed_id
    )


@router.get("/feeds/{feed_id}/trackers")
async def list_trackers(feed: AccessibleFeed, session: DBSession) -> list[TrackerOut]:
    """This feed's trackers, and never another feed's.

    Scoped by `feed.id` after `accessible_feed` has already proven the caller
    may see it, so there is no path here that reads a feed id off the request.
    """
    rows = await session.execute(
        select(Tracker).where(Tracker.feed_id == feed.id).order_by(Tracker.nickname)
    )
    return [
        TrackerOut(nickname=t.nickname, feed_id=t.feed_id) for t in rows.scalars().all()
    ]


@router.get("/feeds/{feed_id}/trackers/{nickname}")
async def read_tracker_by_nickname(
    feed: AccessibleFeed, nickname: str, session: DBSession
) -> TrackerDetailOut:
    """One tracker, addressed the way the frontend is allowed to address it.

    Nickname is not unique in the schema, so two trackers on one feed can
    collide. Resolving to the lowest id is a deterministic answer rather than a
    correct one; phase 5 is where the write path should stop it happening.
    """
    tracker = await session.scalar(
        select(Tracker)
        .where(Tracker.feed_id == feed.id, Tracker.nickname == nickname)
        .order_by(Tracker.id)
    )
    if tracker is None:
        raise HTTPException(status_code=404, detail="Tracker not found")
    return _detail(tracker)


@router.get("/trackers/{tracker_id}")
async def read_tracker(
    tracker_id: str, user_id: CurrentUser, session: DBSession
) -> TrackerDetailOut:
    """One tracker by its credential, for a caller that already holds it."""
    tracker = await session.scalar(
        select(Tracker).where(
            Tracker.feed_id.in_(accessible_feed_ids(user_id)),
            Tracker.id == tracker_id,
        )
    )
    if tracker is None:
        raise HTTPException(status_code=404, detail="Tracker not found")
    return _detail(tracker)
