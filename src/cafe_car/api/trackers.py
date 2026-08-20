"""Tracker endpoints, and the one place the provisioning credential is served.

A tracker's ``id`` is a surrogate and carries nothing secret, so there is one
detail route and it is addressed the same way the client navigates. The secret
is ``device_key``, the Traccar ``uniqueId``, and there is no password behind it,
so it is the whole secret: :class:`TrackerOut` has no such field and only
:class:`TrackerDetailOut`, returned by the detail route alone, does.

``/feeds/{id}/assignments`` expands rules over a date range rather than making
the client re-implement the recurrence logic. The expansion itself lives in
``railroad_club.trip_resolver`` next to ``resolve_tracker_trip``, so the
calendar and the resolver cannot disagree about which day a rule runs.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from railroad_club.models.tracker import Tracker
from railroad_club.models.tracker_rule import TrackerRule, TrackerRuleException
from railroad_club.trip_resolver import expand_rules
from sqlalchemy import select

from cafe_car.admin.access import accessible_feed_ids
from cafe_car.api.deps import AccessibleFeed, CurrentUser, DBSession
from cafe_car.api.schemas import (
    AssignmentOut,
    RuleExceptionOut,
    TrackerDetailOut,
    TrackerOut,
    TrackerRuleOut,
)

router = APIRouter()

# How wide a window the calendar may ask for in one request. A year of daily
# rules is a few thousand rows; anything larger is a client bug, not a view.
MAX_ASSIGNMENT_DAYS = 370


def _out(tracker: Tracker) -> TrackerOut:
    return TrackerOut(id=tracker.id, nickname=tracker.nickname, feed_id=tracker.feed_id)


@router.get("/feeds/{feed_id}/trackers")
async def list_trackers(feed: AccessibleFeed, session: DBSession) -> list[TrackerOut]:
    """This feed's trackers, and never another feed's.

    Scoped by `feed.id` after `accessible_feed` has already proven the caller
    may see it, so there is no path here that reads a feed id off the request.
    """
    rows = await session.execute(
        select(Tracker).where(Tracker.feed_id == feed.id).order_by(Tracker.nickname)
    )
    return [_out(t) for t in rows.scalars().all()]


@router.get("/trackers/{tracker_id}")
async def read_tracker(
    tracker_id: str, user_id: CurrentUser, session: DBSession
) -> TrackerDetailOut:
    """One tracker, including its credential.

    Scoped by `accessible_feed_ids` rather than by a feed id in the path: the
    surrogate is the address, and a tracker on a feed the caller cannot see is
    404 like the feed itself.
    """
    tracker = await session.scalar(
        select(Tracker).where(
            Tracker.feed_id.in_(accessible_feed_ids(user_id)),
            Tracker.id == tracker_id,
        )
    )
    if tracker is None:
        raise HTTPException(status_code=404, detail="Tracker not found")
    return TrackerDetailOut(
        id=tracker.id,
        nickname=tracker.nickname,
        feed_id=tracker.feed_id,
        device_key=tracker.device_key,
    )


async def _feed_rules(
    session: DBSession, feed_id: int
) -> tuple[list[TrackerRule], dict[int, list[TrackerRuleException]], dict[str, str]]:
    """Every rule on a feed, its exceptions, and its trackers' nicknames.

    One join and one `IN` rather than walking `rule.tracker` or
    `rule.exceptions`: those are lazy relationships, and touching them after the
    statement completes raises `MissingGreenlet` under the async session.
    """
    rows = await session.execute(
        select(TrackerRule, Tracker)
        .join(Tracker, TrackerRule.tracker_id == Tracker.id)
        .where(Tracker.feed_id == feed_id)
    )
    rules: list[TrackerRule] = []
    nicknames: dict[str, str] = {}
    for rule, tracker in rows.all():
        rules.append(rule)
        nicknames[tracker.id] = tracker.nickname

    exceptions: dict[int, list[TrackerRuleException]] = {}
    if rules:
        exc_rows = await session.execute(
            select(TrackerRuleException)
            .where(TrackerRuleException.rule_id.in_([r.id for r in rules]))
            .order_by(TrackerRuleException.date)
        )
        for exc in exc_rows.scalars().all():
            exceptions.setdefault(exc.rule_id, []).append(exc)
    return rules, exceptions, nicknames


def _exception_dates(
    exceptions: dict[int, list[TrackerRuleException]],
) -> dict[int, dict[date, str]]:
    """The shape `expand_rules` wants: rule id -> service date -> type."""
    return {
        rule_id: {exc.date: exc.exception_type for exc in rows}
        for rule_id, rows in exceptions.items()
    }


@router.get("/feeds/{feed_id}/rules")
async def list_rules(feed: AccessibleFeed, session: DBSession) -> list[TrackerRuleOut]:
    """The feed's rules as stored, for editing rather than for the calendar."""
    rules, exceptions, _ = await _feed_rules(session, feed.id)
    return [
        TrackerRuleOut(
            id=rule.id,
            tracker_id=rule.tracker_id,
            trip_id=rule.trip_id,
            monday=rule.monday,
            tuesday=rule.tuesday,
            wednesday=rule.wednesday,
            thursday=rule.thursday,
            friday=rule.friday,
            saturday=rule.saturday,
            sunday=rule.sunday,
            start_date=rule.start_date,
            end_date=rule.end_date,
            start_time=rule.start_time,
            end_time=rule.end_time,
            exceptions=[
                RuleExceptionOut(
                    id=exc.id, date=exc.date, exception_type=exc.exception_type
                )
                for exc in exceptions.get(rule.id, [])
            ],
        )
        for rule in sorted(rules, key=lambda r: (r.tracker_id, r.id))
    ]


@router.get("/feeds/{feed_id}/assignments")
async def list_assignments(
    feed: AccessibleFeed,
    session: DBSession,
    from_date: Annotated[date, Query(alias="from")],
    to_date: Annotated[date, Query(alias="to")],
) -> list[AssignmentOut]:
    """Every rule occurrence on this feed between two service dates, inclusive.

    Dates are service dates in feed-local time, which is the same calendar the
    resolver stamps onto a vehicle's ``start_date``. The endpoint does not need
    the feed's timezone to answer: it expands over dates, and only the resolver
    has to know what "now" means locally.
    """
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="`to` is before `from`")
    if (to_date - from_date) > timedelta(days=MAX_ASSIGNMENT_DAYS):
        raise HTTPException(
            status_code=400, detail=f"Range exceeds {MAX_ASSIGNMENT_DAYS} days"
        )

    rules, exceptions, nicknames = await _feed_rules(session, feed.id)
    return [
        AssignmentOut(
            rule_id=a.rule_id,
            tracker_id=a.tracker_id,
            tracker_nickname=nicknames[a.tracker_id],
            trip_id=a.trip_id,
            service_date=a.service_date,
            start_time=a.start_time,
            end_time=a.end_time,
        )
        for a in expand_rules(rules, _exception_dates(exceptions), from_date, to_date)
    ]
