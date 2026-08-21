"""Seed the local dev stack's poller feeds, from inside the compose network.

`docker compose up` should be enough to reach a stack you can look at, which
means the rows hell-gate-bridge's two pollers publish into have to exist before
they start. This is the one-shot container that creates them, gated by
`service_completed_successfully` the same way `migrate` is.

It lives in the package rather than in `scripts/` because the built image
carries neither `scripts/` nor `uv`: a console script on PATH is the only thing
a compose `command:` can reach. That also means it ships inside the production
wheel, so it refuses to run unless DEBUG is set.

The two tracker ids here are fixed strings, not the surrogates a tracker
normally gets, because docker-compose.yml names them literally in each poller's
INGEST_VEHICLE_ID and cafe-car scans `vehicle:{tracker.id}:*`. The two have to
agree; changing one means changing the other.

`west` is deliberately not here. It is a real Traccar device with schedule
rules, and both of those are host concerns: see music-student's
scripts/provision_default_feeds.sh.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple

from railroad_club.models.feed import Feed
from sqlmodel import select

from cafe_car.accounts import resolve_login
from cafe_car.database import get_session_factory
from cafe_car.feed_load import request_feed_load
from cafe_car.keycloak import get_keycloak_client
from cafe_car.provisioning import ProvisionError, upsert_feed, upsert_tracker
from cafe_car.settings import get_settings

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("seed_dev")

# The realm account every seeded feed is owned by. Its subject is minted fresh
# on each realm import, so it is looked up rather than hardcoded.
OWNER_USERNAME = "alice"
OWNER_EMAIL = "alice@local"
OWNER_NAME = "Alice Local"


class SeedFeed(NamedTuple):
    feed_name: str
    static_feed_url: str
    nickname: str
    tracker_id: str


SEED_FEEDS = (
    SeedFeed(
        feed_name="amtrak",
        static_feed_url="https://content.amtrak.com/content/gtfs/GTFS.zip",
        nickname="Amtrak",
        tracker_id="amtrak-live",
    ),
    SeedFeed(
        feed_name="columbia-county",
        static_feed_url=(
            "https://raw.githubusercontent.com/columbia-county-ny-transit/"
            "gtfs-generator/refs/heads/main/columbia_county_gtfs.zip"
        ),
        nickname="Columbia County",
        tracker_id="columbia-county",
    ),
)


async def _owner_subject() -> str:
    """Alice's Keycloak subject, read from the realm at runtime."""
    client = get_keycloak_client()
    if client is None:
        raise ProvisionError(
            "No Keycloak client configured; set KEYCLOAK_CLIENT_ID and "
            "KEYCLOAK_CLIENT_SECRET on this service."
        )
    subject = await client.subject_for_username(OWNER_USERNAME)
    if subject is None:
        raise ProvisionError(
            f"Realm {get_settings().keycloak_realm!r} has no user "
            f"{OWNER_USERNAME!r}; the realm import has not run."
        )
    return subject


async def _seed(session: AsyncSession) -> list[int]:
    """Create the owner and both feeds. Returns the ids of new feeds."""
    subject = await _owner_subject()
    # The same call a real login makes, so a later browser sign-in lands on this
    # account instead of minting a second one.
    owner = (
        await resolve_login(
            session,
            provider=get_settings().oidc_provider,
            subject=subject,
            email=OWNER_EMAIL,
            email_verified=True,
            display_name=OWNER_NAME,
        )
    ).user
    log.info("Owner %s (id=%s, subject=%s)", OWNER_EMAIL, owner.id, subject)

    created: list[int] = []
    for spec in SEED_FEEDS:
        before = await session.scalar(
            select(Feed.id).where(Feed.feed_name == spec.feed_name)
        )
        feed = await upsert_feed(
            session,
            feed_name=spec.feed_name,
            static_feed_url=spec.static_feed_url,
            owner_id=owner.id,
            update_url=False,
        )
        await upsert_tracker(
            session,
            feed_id=feed.id,
            nickname=spec.nickname,
            device_key=None,
            tracker_id=spec.tracker_id,
        )
        if before is None:
            created.append(feed.id)
    return created


async def seed() -> int:
    factory = get_session_factory()
    async with factory() as session:
        created = await _seed(session)
        await session.commit()
    # After the commit: a queued load that races the transaction would find no
    # feed. Never raises, so a cold broker cannot fail the seed.
    for feed_id in created:
        request_feed_load(feed_id)
    log.info("Seeded %d feed(s), %d newly created", len(SEED_FEEDS), len(created))
    return 0


def main() -> int:
    if not get_settings().debug:
        log.error("seed_dev is a dev fixture and refuses to run without DEBUG set.")
        return 1
    try:
        return asyncio.run(seed())
    except ProvisionError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
