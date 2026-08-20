"""Tracker endpoints, and the one place the provisioning credential is served.

A tracker's ``id`` is a surrogate and carries nothing secret, so there is one
detail route and it is addressed the same way the client navigates. The secret
is ``device_key``, the Traccar ``uniqueId``, and there is no password behind it,
so it is the whole secret: :class:`TrackerOut` has no such field and only
:class:`TrackerDetailOut`, returned by the detail route alone, does.

Creating and renaming are open to any member, matching the admin this
replaces: a tracker is a piece of the feed's equipment rather than a piece of
its ownership. Deleting is too, and it takes the Traccar device with it.

``/feeds/{id}/assignments`` expands rules over a date range rather than making
the client re-implement the recurrence logic. The expansion itself lives in
``railroad_club.trip_resolver`` next to ``resolve_tracker_trip``, so the
calendar and the resolver cannot disagree about which day a rule runs.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from railroad_club.models.tracker import Tracker, generate_device_key
from railroad_club.models.tracker_rule import TrackerRule, TrackerRuleException
from railroad_club.trip_resolver import expand_rules
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from cafe_car.admin.access import accessible_feed_ids
from cafe_car.api.deps import AccessibleFeed, CurrentUser, DBSession
from cafe_car.api.schemas import (
    AssignmentOut,
    ProvisioningOut,
    RuleExceptionOut,
    TrackerBulkCreate,
    TrackerCreate,
    TrackerDetailOut,
    TrackerOut,
    TrackerRuleOut,
    TrackerUpdate,
)
from cafe_car.traccar import build_config_url, provision_device, qr_svg, retire_device

router = APIRouter()

# How wide a window the calendar may ask for in one request. A year of daily
# rules is a few thousand rows; anything larger is a client bug, not a view.
MAX_ASSIGNMENT_DAYS = 370

# How many trackers one bulk create may make. A fleet is tens, not thousands,
# and each one provisions a Traccar device.
MAX_BULK_TRACKERS = 100


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


async def _accessible_tracker(
    session: DBSession, user_id: int, tracker_id: str
) -> Tracker:
    """A tracker on a feed the caller may see, or 404.

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
    return tracker


def _detail(tracker: Tracker) -> TrackerDetailOut:
    return TrackerDetailOut(
        id=tracker.id,
        nickname=tracker.nickname,
        feed_id=tracker.feed_id,
        device_key=tracker.device_key,
    )


@router.get("/trackers/{tracker_id}")
async def read_tracker(
    tracker_id: str, user_id: CurrentUser, session: DBSession
) -> TrackerDetailOut:
    """One tracker, including its credential."""
    return _detail(await _accessible_tracker(session, user_id, tracker_id))


@router.get("/trackers/{tracker_id}/provisioning")
async def read_provisioning(
    tracker_id: str, user_id: CurrentUser, session: DBSession
) -> ProvisioningOut:
    """The Traccar config URL and its QR, built from the credential.

    Every field here contains `device_key`, the QR included, so this response
    is exactly as secret as the detail one. It is a separate route only because
    rendering a QR is work no list or detail view should pay for.
    """
    tracker = await _accessible_tracker(session, user_id, tracker_id)
    config_url = build_config_url(tracker.device_key)
    return ProvisioningOut(
        device_key=tracker.device_key,
        config_url=config_url,
        qr_svg=qr_svg(config_url),
    )


@router.post("/feeds/{feed_id}/trackers", status_code=201)
async def create_tracker(
    payload: TrackerCreate, feed: AccessibleFeed, session: DBSession
) -> TrackerDetailOut:
    """Create one tracker and provision its Traccar device.

    The response is the detail form: whoever just made a tracker is about to
    provision it, and a second round trip for the credential would buy nothing.
    `(feed_id, nickname)` is unique, so a repeated nickname is a 409 the form
    can show against the field rather than an IntegrityError.
    """
    tracker = Tracker(
        feed_id=feed.id,
        nickname=payload.nickname,
        device_key=payload.device_key or generate_device_key(),
    )
    session.add(tracker)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"This feed already has a tracker called {payload.nickname}",
        ) from None
    await session.refresh(tracker)

    await provision_device(tracker.nickname, tracker.device_key)
    return _detail(tracker)


@router.post("/feeds/{feed_id}/trackers/bulk", status_code=201)
async def create_trackers(
    payload: TrackerBulkCreate, feed: AccessibleFeed, session: DBSession
) -> list[TrackerOut]:
    """Create several trackers at once, named `{prefix}{n}`.

    Numbering continues past the highest `{prefix}{n}` the feed already has, so
    running the same bulk create twice extends the fleet rather than colliding
    with it. The summary form is returned, not the detail one: a device key is
    fetched per tracker, when its own page is opened.
    """
    if payload.count < 1 or payload.count > MAX_BULK_TRACKERS:
        raise HTTPException(
            status_code=422, detail=f"Create between 1 and {MAX_BULK_TRACKERS} trackers"
        )

    existing = (
        (
            await session.execute(
                select(Tracker.nickname).where(
                    Tracker.feed_id == feed.id,
                    Tracker.nickname.startswith(payload.prefix),
                )
            )
        )
        .scalars()
        .all()
    )
    taken = set(existing)
    highest = 0
    for nickname in existing:
        suffix = nickname[len(payload.prefix) :]
        if suffix.isdigit():
            highest = max(highest, int(suffix))

    created: list[Tracker] = []
    n = highest
    while len(created) < payload.count:
        n += 1
        nickname = f"{payload.prefix}{n}"
        # A non-numeric collision (`bus-2a` is not counted above) skips rather
        # than failing the whole batch.
        if nickname in taken:
            continue
        created.append(Tracker(feed_id=feed.id, nickname=nickname))

    session.add_all(created)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Those names collide with existing trackers"
        ) from None

    for tracker in created:
        await session.refresh(tracker)
        await provision_device(tracker.nickname, tracker.device_key)
    return [_out(t) for t in created]


@router.patch("/trackers/{tracker_id}")
async def update_tracker(
    payload: TrackerUpdate, tracker_id: str, user_id: CurrentUser, session: DBSession
) -> TrackerOut:
    """Rename a tracker.

    A rename is free of navigation consequences - the hash holds the surrogate,
    so a live link survives it - but `(feed_id, nickname)` is still unique and a
    collision is a 409 against the field. The Traccar device is *not* renamed:
    it is keyed by `device_key`, and its name is a label in a system this app
    does not own.
    """
    tracker = await _accessible_tracker(session, user_id, tracker_id)
    tracker.nickname = payload.nickname
    session.add(tracker)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"This feed already has a tracker called {payload.nickname}",
        ) from None
    await session.refresh(tracker)
    return _out(tracker)


@router.delete("/trackers/{tracker_id}", status_code=204)
async def delete_tracker(
    tracker_id: str, user_id: CurrentUser, session: DBSession
) -> Response:
    """Delete a tracker, its assignment rules, and its Traccar device.

    The rules go first because they point at it. The device is retired after
    the commit and best-effort: the row is gone whatever Traccar says, and a
    credential no tracker answers for is exactly what was asked for.
    """
    tracker = await _accessible_tracker(session, user_id, tracker_id)
    device_key = tracker.device_key

    rule_ids = (
        (
            await session.execute(
                select(TrackerRule.id).where(TrackerRule.tracker_id == tracker.id)
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
        await session.execute(delete(TrackerRule).where(TrackerRule.id.in_(rule_ids)))
    await session.delete(tracker)
    await session.commit()

    await retire_device(device_key)
    return Response(status_code=204)


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
