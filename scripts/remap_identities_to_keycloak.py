#!/usr/bin/env python3
"""Repoint existing `identity` rows at Keycloak, for the Dex→Keycloak cutover.

Every identity in production today is keyed on ``(provider="dex",
provider_subject=<whatever oauth2-proxy passed>)``. After the cutover
oauth2-proxy sends a Keycloak UUID instead, so those rows match nothing: each
person would silently get a brand-new empty account while their feeds stayed
attached to the orphaned ``user.id``. This script rewrites the rows in place so
the first post-cutover login lands on the account they already had.

It is a one-shot script rather than an Alembic migration because matching a row
means asking Keycloak who the person is, and a migration has no business making
HTTP calls.

Matching, in order of preference:

1. The Keycloak user with a **federated identity** whose upstream user id or
   username equals the row's ``provider_subject``. This is the strong match:
   Dex's GitHub connector and Keycloak's GitHub IdP both key on the same
   GitHub account.
2. Failing that, the Keycloak user whose email equals the row's email, but
   *only* if Keycloak has that address marked verified and it is unambiguous.

Anything unmatched aborts the run. A partial remap is worse than none: the
people who were missed cannot tell that anything is wrong, they just find an
empty account.

Usage (dry run first, always):

    uv run scripts/remap_identities_to_keycloak.py \\
        --keycloak-url https://id.gtfs.zone --realm gtfs \\
        --admin-user admin --admin-password "$KC_ADMIN_PASSWORD"

    ... --apply     # after reading the plan it prints
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import TYPE_CHECKING, Any

import httpx
from gtfs_zone_db_models.models.identity import Identity
from sqlmodel import select

from gtfs_zone_rt_api.database import get_session_factory

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logging.basicConfig(level=logging.INFO, format="%(message)s")
# One line per admin-API call would bury the plan, which is the whole output.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("remap")

NEW_PROVIDER = "keycloak"


class Keycloak:
    """The slice of the admin API this script needs."""

    def __init__(self, base_url: str, realm: str, client: httpx.AsyncClient) -> None:
        self.base = base_url.rstrip("/")
        self.realm = realm
        self.client = client
        self._token: str | None = None

    async def login(self, username: str, password: str) -> None:
        response = await self.client.post(
            f"{self.base}/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": username,
                "password": password,
            },
        )
        response.raise_for_status()
        self._token = response.json()["access_token"]

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def users(self) -> list[dict[str, Any]]:
        """Every user in the realm.

        Paged deliberately rather than trusting one big request: the default
        server-side cap is 100 and silently truncating the list is exactly the
        failure mode that leaves someone behind.
        """
        out: list[dict[str, Any]] = []
        first, batch = 0, 100
        while True:
            response = await self.client.get(
                f"{self.base}/admin/realms/{self.realm}/users",
                params={"first": first, "max": batch},
                headers=self._headers,
            )
            response.raise_for_status()
            page = response.json()
            out.extend(page)
            if len(page) < batch:
                return out
            first += batch

    async def federated_identities(self, user_id: str) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"{self.base}/admin/realms/{self.realm}/users/{user_id}/federated-identity",
            headers=self._headers,
        )
        response.raise_for_status()
        return response.json()


async def _build_index(kc: Keycloak) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return (upstream-key → kc sub, verified email → [kc sub]).

    The first index is what makes the strong match possible: it holds every
    upstream user id and username Keycloak knows about, across all brokered
    providers.
    """
    by_upstream: dict[str, str] = {}
    by_email: dict[str, list[str]] = {}

    for user in await kc.users():
        sub = user["id"]
        if user.get("email") and user.get("emailVerified"):
            by_email.setdefault(user["email"].strip().lower(), []).append(sub)
        for link in await kc.federated_identities(sub):
            for key in (link.get("userId"), link.get("userName")):
                if key:
                    by_upstream.setdefault(key, sub)

    return by_upstream, by_email


def _match(
    identity: Identity,
    by_upstream: dict[str, str],
    by_email: dict[str, list[str]],
) -> tuple[str | None, str]:
    """Return (keycloak sub, how it was found)."""
    strong = by_upstream.get(identity.provider_subject)
    if strong:
        return strong, "federated identity"

    if identity.email:
        subs = by_email.get(identity.email.strip().lower(), [])
        if len(subs) == 1:
            return subs[0], "verified email"
        if len(subs) > 1:
            return None, f"AMBIGUOUS: {len(subs)} Keycloak users share this email"

    return None, "no Keycloak user found"


async def _load_identities(session: AsyncSession) -> list[Identity]:
    # `exec`, not `execute`: SQLModel prints a multi-line deprecation banner for
    # the latter, which would bury the plan this script exists to print.
    result = await session.exec(select(Identity).order_by(Identity.id))
    return list(result.all())


async def run(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(timeout=30.0) as http:
        kc = Keycloak(args.keycloak_url, args.realm, http)
        await kc.login(args.admin_user, args.admin_password)
        logger.info("Indexing Keycloak realm %r…", args.realm)
        by_upstream, by_email = await _build_index(kc)
        logger.info(
            "  %d upstream keys, %d verified addresses",
            len(by_upstream),
            len(by_email),
        )

    async with get_session_factory()() as session:
        identities = await _load_identities(session)
        if not identities:
            logger.info("No identity rows. Nothing to do.")
            return 0

        planned: list[tuple[Identity, str, str]] = []
        unmatched: list[tuple[Identity, str]] = []

        # Rows already pointing at a real Keycloak subject are done; this is
        # what makes the script safe to re-run after a partial cutover.
        known_subs = set(by_upstream.values()) | {
            sub for subs in by_email.values() for sub in subs
        }

        for identity in identities:
            if (
                identity.provider == NEW_PROVIDER
                and identity.provider_subject in known_subs
            ):
                logger.info(
                    "  identity %-4s user %-4s already on Keycloak, skipping",
                    identity.id,
                    identity.user_id,
                )
                continue
            sub, how = _match(identity, by_upstream, by_email)
            if sub is None:
                unmatched.append((identity, how))
            else:
                planned.append((identity, sub, how))

        logger.info("\nPlan:")
        for identity, sub, how in planned:
            logger.info(
                "  identity %-4s user %-4s  (%s, %s)  ->  (keycloak, %s)   [%s]",
                identity.id,
                identity.user_id,
                identity.provider,
                identity.provider_subject,
                sub,
                how,
            )
        for identity, why in unmatched:
            logger.error(
                "  identity %-4s user %-4s  (%s, %s)  email=%s  ->  UNMATCHED: %s",
                identity.id,
                identity.user_id,
                identity.provider,
                identity.provider_subject,
                identity.email,
                why,
            )

        # Two rows mapping to one Keycloak user would violate
        # UNIQUE(provider, provider_subject), and it means two rt-api
        # principals for one person, which is a merge decision, not a remap.
        by_sub: dict[str, list[Identity]] = {}
        for identity, sub, _ in planned:
            by_sub.setdefault(sub, []).append(identity)
        collisions = [(sub, rows) for sub, rows in by_sub.items() if len(rows) > 1]
        for sub, rows in collisions:
            logger.error(
                "  COLLISION: Keycloak %s is claimed by identities %s "
                "(users %s), merge those users first (/account, or merge_users)",
                sub,
                ", ".join(str(r.id) for r in rows),
                ", ".join(str(uid) for uid in sorted({r.user_id for r in rows})),
            )

        logger.info(
            "\n%d to remap, %d unmatched, %d colliding subject%s.",
            len(planned),
            len(unmatched),
            len(collisions),
            "" if len(collisions) == 1 else "s",
        )

        if unmatched or collisions:
            logger.error(
                "\nRefusing to write. A missed row is silent: that person gets a "
                "fresh empty account while their feeds stay on the old user id.\n"
                "Import the missing people into Keycloak (or merge the colliding "
                "accounts) and run again."
            )
            return 1

        if not args.apply:
            logger.info("\nDry run. Re-run with --apply to write.")
            return 0

        for identity, sub, _ in planned:
            identity.provider = NEW_PROVIDER
            identity.provider_subject = sub
        await session.commit()
        logger.info("\nRemapped %d identities.", len(planned))
        logger.info(
            "Now flush oauth2-proxy's sessions (Redis DB 0): every one of them "
            "references a Dex token."
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keycloak-url", required=True, help="e.g. https://id.gtfs.zone"
    )
    parser.add_argument("--realm", default="gtfs")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("KC_ADMIN_PASSWORD"),
        help="defaults to $KC_ADMIN_PASSWORD",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write; without it the script only prints the plan",
    )
    args = parser.parse_args()

    if not args.admin_password:
        parser.error("--admin-password or $KC_ADMIN_PASSWORD is required")

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
