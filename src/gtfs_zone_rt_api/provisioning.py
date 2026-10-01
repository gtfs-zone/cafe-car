"""Creating the Feed / Tracker / TrackerRule rows a producer needs.

Shared by ``scripts/provision_source.py``, the host CLI, and
``gtfs_zone_rt_api.seed_dev``, the container that seeds the dev stack. One definition,
because the last time these rows were described twice the second copy drifted
behind a ``Tracker`` model change and stopped working.

Every helper is idempotent: it reuses the row it would otherwise create.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gtfs_zone_db_models.models.feed import Feed
from gtfs_zone_db_models.models.identity import Identity
from gtfs_zone_db_models.models.tracker import Tracker
from gtfs_zone_db_models.models.tracker_rule import TrackerRule
from gtfs_zone_db_models.models.user import User
from sqlmodel import delete, select

if TYPE_CHECKING:
    from datetime import date

    from sqlmodel.ext.asyncio.session import AsyncSession

log = logging.getLogger(__name__)


# TrackerRule weekday columns, Monday-first (matches datetime.weekday()).
_WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
_ABBR = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class ProvisionError(Exception):
    """A user-facing failure that should exit non-zero with a clear message."""


def _parse_days(spec: str) -> set[int]:
    """Parse a day spec into weekday indices (Mon=0..Sun=6).

    Accepts ``daily``, comma lists (``mon,wed,fri``), and ranges (``mon-fri``).
    """
    spec = spec.strip().lower()
    if spec in ("daily", "all", "everyday"):
        return set(range(7))
    days: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, end = token.split("-", 1)
            if start not in _ABBR or end not in _ABBR:
                raise ProvisionError(f"Unknown day in range: {token!r}")
            lo, hi = _ABBR[start], _ABBR[end]
            if lo > hi:
                raise ProvisionError(f"Reversed day range: {token!r}")
            days.update(range(lo, hi + 1))
        else:
            if token not in _ABBR:
                raise ProvisionError(f"Unknown day: {token!r}")
            days.add(_ABBR[token])
    if not days:
        raise ProvisionError(f"No days parsed from {spec!r}")
    return days


def _parse_time(text: str) -> int:
    """Parse ``HH:MM`` to seconds since service midnight; hours may exceed 24."""
    try:
        hh, mm = text.split(":", 1)
        hours, minutes = int(hh), int(mm)
    except (ValueError, TypeError) as exc:
        raise ProvisionError(f"Bad time {text!r}; expected HH:MM") from exc
    if hours < 0 or not 0 <= minutes < 60:
        raise ProvisionError(f"Bad time {text!r}; expected HH:MM")
    return hours * 3600 + minutes * 60


def _parse_rule(raw: str) -> tuple[set[int], int, int, str]:
    """Parse ``DAYS=HH:MM-HH:MM=TRIP_ID`` into (days, start, end, trip_id)."""
    parts = raw.split("=")
    if len(parts) != 3:
        raise ProvisionError(f"Bad --rule {raw!r}; expected DAYS=HH:MM-HH:MM=TRIP_ID")
    days_spec, window, trip_id = parts
    if "-" not in window:
        raise ProvisionError(f"Bad time window {window!r}; expected HH:MM-HH:MM")
    start_text, end_text = window.split("-", 1)
    days = _parse_days(days_spec)
    start, end = _parse_time(start_text), _parse_time(end_text)
    if end <= start:
        raise ProvisionError(f"Rule end {end_text} must be after start {start_text}")
    if not trip_id.strip():
        raise ProvisionError(f"Empty trip_id in --rule {raw!r}")
    return days, start, end, trip_id.strip()


def _build_rules(
    tracker_id: str,
    raw_rules: list[str],
    start_date: date,
    end_date: date | None,
) -> list[TrackerRule]:
    rules: list[TrackerRule] = []
    for raw in raw_rules:
        days, start, end, trip_id = _parse_rule(raw)
        weekday_flags = {col: (i in days) for i, col in enumerate(_WEEKDAYS)}
        rules.append(
            TrackerRule(
                tracker_id=tracker_id,
                trip_id=trip_id,
                start_date=start_date,
                end_date=end_date,
                start_time=start,
                end_time=end,
                **weekday_flags,
            )
        )
    return rules


async def find_owner(
    session: AsyncSession, provider: str, email: str, subject: str | None
) -> User:
    if subject is not None:
        stmt = (
            select(User)
            .join(Identity, Identity.user_id == User.id)
            .where(Identity.provider == provider, Identity.provider_subject == subject)
        )
        who = f"identity provider={provider!r} subject={subject!r}"
    else:
        stmt = select(User).where(User.primary_email == email)
        who = f"primary_email={email!r}"
    owner = await session.scalar(stmt)
    if owner is None:
        raise ProvisionError(
            f"No User with {who}. Log into the admin as that user once (users "
            "are created lazily on first login), or pass --owner-subject for a "
            "user that already exists under a different email."
        )
    return owner


async def upsert_feed(
    session: AsyncSession,
    *,
    feed_name: str,
    static_feed_url: str | None,
    owner_id: int,
    update_url: bool,
) -> Feed:
    feed = await session.scalar(select(Feed).where(Feed.feed_name == feed_name))
    if feed is None:
        if not static_feed_url:
            raise ProvisionError(
                f"Feed {feed_name!r} does not exist; --static-feed-url is required "
                "to create it."
            )
        feed = Feed(
            feed_name=feed_name,
            static_feed_url=static_feed_url,
            owner_id=owner_id,
        )
        session.add(feed)
        await session.flush()  # assign feed.id
        log.info("Created feed %r (id=%s)", feed_name, feed.id)
    else:
        log.info("Reusing feed %r (id=%s)", feed_name, feed.id)
        if static_feed_url and update_url and feed.static_feed_url != static_feed_url:
            feed.static_feed_url = static_feed_url
            session.add(feed)
            log.info("Updated static_feed_url for %r", feed_name)
    return feed


async def upsert_tracker(
    session: AsyncSession,
    *,
    feed_id: int,
    nickname: str,
    device_key: str | None,
    tracker_id: str | None = None,
) -> Tracker:
    """Reuse or create a tracker.

    ``tracker_id`` pins the surrogate primary key instead of letting the model's
    default factory generate one. Only a fixture whose id is hardcoded somewhere
    else should pass it: everything user-facing addresses a tracker by whatever
    id it was given. A tracker that already exists under this nickname with a
    *different* id is an error rather than a silent reuse, because the caller
    that pinned the id is going to keep writing to the id it asked for.
    """
    tracker: Tracker | None = None
    if tracker_id is not None:
        tracker = await session.get(Tracker, tracker_id)
    if tracker is None and device_key is not None:
        tracker = await session.scalar(
            select(Tracker).where(Tracker.device_key == device_key)
        )
    if tracker is None:
        tracker = await session.scalar(
            select(Tracker).where(
                Tracker.feed_id == feed_id, Tracker.nickname == nickname
            )
        )
    if tracker is not None and tracker_id is not None and tracker.id != tracker_id:
        raise ProvisionError(
            f"Tracker {nickname!r} already exists with id {tracker.id!r}, not the "
            f"requested {tracker_id!r}. Rules and positions hang off the old id, "
            "so this is not something to rewrite in place: wipe the stack with "
            "`docker compose down -v` and let it be recreated."
        )
    if tracker is None:
        kwargs = {"nickname": nickname, "feed_id": feed_id}
        if device_key is not None:
            kwargs["device_key"] = device_key
        if tracker_id is not None:
            kwargs["id"] = tracker_id
        tracker = Tracker(**kwargs)
        session.add(tracker)
        # Trigger the id / device_key default factories when not supplied.
        await session.flush()
        log.info("Created tracker id=%s nickname=%r", tracker.id, nickname)
    else:
        log.info("Reusing tracker id=%s nickname=%r", tracker.id, tracker.nickname)
    return tracker


async def replace_rules(
    session: AsyncSession,
    tracker_id: str,
    raw_rules: list[str],
    start_date: date,
    end_date: date | None,
) -> int:
    """Replace all rules for the tracker with the provided set (idempotent)."""
    await session.exec(delete(TrackerRule).where(TrackerRule.tracker_id == tracker_id))
    rules = _build_rules(tracker_id, raw_rules, start_date, end_date)
    for rule in rules:
        session.add(rule)
    log.info("Set %d rule(s) for tracker %s", len(rules), tracker_id)
    return len(rules)
