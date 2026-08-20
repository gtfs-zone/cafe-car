#!/usr/bin/env python3
"""Provision a feed source (Feed + Tracker) in the cafe-car database.

Creates the row chain a producer needs to surface in a GTFS-RT feed: a `Feed`
(owned by an existing `User`) and a `Tracker`. The tracker has two identities:
`id`, a surrogate that is the Redis key and is not secret, and `device_key`, the
Traccar `uniqueId`, which is. After the DB upsert it creates the matching Traccar
device via REST from the `device_key`, then prints the surrogate `id` to paste
into the producer's env (e.g. hell-gate-bridge `INGEST_VEHICLE_ID`).

Because `gtfs_rt.py` scans `vehicle:{tracker.id}:*`, the producer MUST publish
under `tracker_id == <tracker.id>` (the surrogate) for its positions to appear in
the feed.

The script is idempotent: re-running with the same `--feed-name`/`--nickname`
reuses the existing rows and Traccar device rather than duplicating them.

The final Docker image doesn't include `scripts/` or `uv`, so run this from a
host checkout against the published ports instead of `docker compose exec`:

    cd cafe-car && uv run python scripts/provision_source.py \\
        --feed-name amtrak --static-feed-url https://example.com/amtrak.zip \\
        --nickname "Amtrak NE Regional"

Optional schedule-based trip resolution (not used by producers that post an
explicit trip_id, e.g. hell-gate-bridge, but used by real Traccar devices):

    ... --rule mon-fri=08:00-17:00=AMTK123 --rule sat,sun=10:00-14:00=AMTK199

Rule times are service-relative, so an hour past 24 is how a window that runs
into the next calendar day is written: `--rule fri=23:00-25:30=OWL1`. Rules start
today and are open-ended unless `--rule-start` / `--rule-end` say otherwise.

The owner defaults to `alice@local`, looked up by `User.primary_email`; that
`User` row only exists after she has logged into the admin at least once
(users are created lazily on first authenticated request, one per identity
provider they've never used before, see `railroad_club.models.identity`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date
from typing import TYPE_CHECKING

import httpx
from railroad_club.models.feed import Feed
from railroad_club.models.identity import Identity
from railroad_club.models.tracker import Tracker
from railroad_club.models.tracker_rule import TrackerRule
from railroad_club.models.user import User
from sqlmodel import delete, select

from cafe_car.database import get_session_factory
from cafe_car.traccar import get_traccar_client

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("provision_source")

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


async def _find_owner(
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


async def _upsert_feed(
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


async def _upsert_tracker(
    session: AsyncSession, *, feed_id: int, nickname: str, device_key: str | None
) -> Tracker:
    tracker: Tracker | None = None
    if device_key is not None:
        tracker = await session.scalar(
            select(Tracker).where(Tracker.device_key == device_key)
        )
    if tracker is None:
        tracker = await session.scalar(
            select(Tracker).where(
                Tracker.feed_id == feed_id, Tracker.nickname == nickname
            )
        )
    if tracker is None:
        kwargs = {"nickname": nickname, "feed_id": feed_id}
        if device_key is not None:
            kwargs["device_key"] = device_key
        tracker = Tracker(**kwargs)
        session.add(tracker)
        # Trigger the id / device_key default factories when not supplied.
        await session.flush()
        log.info("Created tracker id=%s nickname=%r", tracker.id, nickname)
    else:
        log.info("Reusing tracker id=%s nickname=%r", tracker.id, tracker.nickname)
    return tracker


async def _replace_rules(
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


async def provision(args: argparse.Namespace) -> int:
    factory = get_session_factory()

    async with factory() as session:
        owner = await _find_owner(
            session, args.owner_provider, args.owner_email, args.owner_subject
        )
        feed = await _upsert_feed(
            session,
            feed_name=args.feed_name,
            static_feed_url=args.static_feed_url,
            owner_id=owner.id,
            update_url=args.update_url,
        )
        tracker = await _upsert_tracker(
            session,
            feed_id=feed.id,
            nickname=args.nickname,
            device_key=args.device_key,
        )
        if args.rule:
            await _replace_rules(
                session,
                tracker.id,
                args.rule,
                args.rule_start or date.today(),
                args.rule_end,
            )
        await session.commit()
        # Capture before the session closes / attributes expire.
        tracker_id, tracker_nick = tracker.id, tracker.nickname
        device_key = tracker.device_key
        feed_name = feed.feed_name

    if not args.skip_traccar:
        try:
            device = await get_traccar_client().ensure_device(
                name=tracker_nick, unique_id=device_key
            )
            log.info(
                "Traccar device ready: id=%s uniqueId=%s",
                device.get("id"),
                device.get("uniqueId"),
            )
        except (httpx.HTTPError, OSError) as exc:
            # Best-effort, mirroring TrackerAdmin.after_model_change: the DB rows
            # are committed regardless; the device can be created later.
            log.warning("Traccar device creation failed (non-fatal): %s", exc)

    print()
    print(f"# Provisioned feed {feed_name!r} / tracker {tracker_nick!r}")
    print(f"# gtfs_rt scans vehicle:{tracker_id}:*")
    print(f"INGEST_VEHICLE_ID={tracker_id}")
    print("# Traccar uniqueId (secret, for a real device only):")
    print(f"# {device_key}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision a Feed + Tracker (and Traccar device) in cafe-car.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--feed-name", required=True, help="Feed name (^[a-z][a-z0-9_-]{2,63}$)"
    )
    parser.add_argument(
        "--static-feed-url",
        help="GTFS static zip URL (required when creating a new feed)",
    )
    parser.add_argument("--nickname", required=True, help="Public tracker label")
    parser.add_argument(
        "--device-key",
        help="Fixed Traccar uniqueId (secret). Defaults to a generated pet name.",
    )
    parser.add_argument(
        "--rule-start",
        type=date.fromisoformat,
        help="First service date rules apply to, YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--rule-end",
        type=date.fromisoformat,
        help="Last service date rules apply to, YYYY-MM-DD (default: open-ended)",
    )
    parser.add_argument(
        "--owner-email",
        default="alice@local",
        help="Owner lookup by User.email (default: alice@local)",
    )
    parser.add_argument(
        "--owner-subject",
        help="Owner lookup by Identity.provider_subject (overrides --owner-email)",
    )
    parser.add_argument(
        "--owner-provider",
        default="keycloak",
        help="Identity.provider to match --owner-subject against (default: keycloak)",
    )
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="DAYS=HH:MM-HH:MM=TRIP_ID",
        help="Schedule rule; repeatable. Re-running with rules replaces all rules.",
    )
    parser.add_argument(
        "--update-url",
        action="store_true",
        help="Update static_feed_url on an existing feed",
    )
    parser.add_argument(
        "--skip-traccar",
        action="store_true",
        help="Skip creating the matching Traccar device",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(provision(args))
    except ProvisionError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
