#!/usr/bin/env python3
"""Provision a feed source (Feed + Tracker) in the rt-api database.

Creates the row chain a producer needs to surface in a GTFS-RT feed: a `Feed`
(owned by an existing `User`) and a `Tracker`. The tracker has two identities:
`id`, a surrogate that is the Redis key and is not secret, and `device_key`, the
Traccar `uniqueId`, which is. After the DB upsert it creates the matching Traccar
device via REST from the `device_key`, then prints the surrogate `id` to paste
into the producer's env (e.g. rt-pollers `INGEST_VEHICLE_ID`).

Because `gtfs_rt.py` scans `vehicle:{tracker.id}:*`, the producer MUST publish
under `tracker_id == <tracker.id>` (the surrogate) for its positions to appear in
the feed.

The script is idempotent: re-running with the same `--feed-name`/`--nickname`
reuses the existing rows and Traccar device rather than duplicating them.

The final Docker image doesn't include `scripts/` or `uv`, so run this from a
host checkout against the published ports instead of `docker compose exec`:

    cd rt-api && uv run python scripts/provision_source.py \\
        --feed-name amtrak --static-feed-url https://example.com/amtrak.zip \\
        --nickname "Amtrak NE Regional"

Optional schedule-based trip resolution (not used by producers that post an
explicit trip_id, e.g. rt-pollers, but used by real Traccar devices):

    ... --rule mon-fri=08:00-17:00=AMTK123 --rule sat,sun=10:00-14:00=AMTK199

Rule times are service-relative, so an hour past 24 is how a window that runs
into the next calendar day is written: `--rule fri=23:00-25:30=OWL1`. Rules start
today and are open-ended unless `--rule-start` / `--rule-end` say otherwise.

The owner defaults to `alice@local`, looked up by `User.primary_email`; that
`User` row only exists after she has logged into the admin at least once
(users are created lazily on first authenticated request, one per identity
provider they've never used before, see `gtfs_zone_db_models.models.identity`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date

import httpx

from gtfs_zone_rt_api.database import get_session_factory
from gtfs_zone_rt_api.provisioning import (
    ProvisionError,
    find_owner,
    replace_rules,
    upsert_feed,
    upsert_tracker,
)
from gtfs_zone_rt_api.traccar import get_traccar_client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("provision_source")



async def provision(args: argparse.Namespace) -> int:
    factory = get_session_factory()

    async with factory() as session:
        owner = await find_owner(
            session, args.owner_provider, args.owner_email, args.owner_subject
        )
        feed = await upsert_feed(
            session,
            feed_name=args.feed_name,
            static_feed_url=args.static_feed_url,
            owner_id=owner.id,
            update_url=args.update_url,
        )
        tracker = await upsert_tracker(
            session,
            feed_id=feed.id,
            nickname=args.nickname,
            device_key=args.device_key,
        )
        if args.rule:
            await replace_rules(
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
        description="Provision a Feed + Tracker (and Traccar device) in rt-api.",
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
