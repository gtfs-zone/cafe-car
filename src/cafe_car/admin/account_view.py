"""The signed-in person's own account page.

A :class:`BaseView` rather than a route on ``entity_router`` so it renders
inside the admin chrome and picks up a nav entry — and so ``@expose``'s
``login_required`` runs the auth backend for it. The two mutating routes it
posts to live in ``entity_router`` alongside the other permission-checked
actions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from railroad_club.models.identity import Identity
from sqladmin import BaseView, expose
from sqlalchemy import select

from cafe_car.accounts import link_candidates
from cafe_car.admin.context import current_user_id_var
from cafe_car.database import get_session_factory
from cafe_car.keycloak import get_keycloak_client
from cafe_car.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)


# What the upstream identity provider is called, for the aliases the brokers
# are configured under. An unknown alias is shown raw rather than hidden — a
# provider nobody has labelled yet is still worth seeing.
BROKER_LABELS = {
    "github": "GitHub",
    "google": "Google",
    "gitlab": "GitLab",
}


async def _linked_providers(
    identities: Sequence[Identity],
) -> tuple[dict[str, list[str]], set[str], bool]:
    """Ask Keycloak which providers each credential can be used with.

    Returns ``(subject → broker aliases, subjects Keycloak no longer has,
    whether the lookup actually ran)``. The third value is what lets the page
    tell "linked to nothing" apart from "could not ask", which look identical
    otherwise and mean very different things.

    Best-effort throughout: the account page is still worth rendering when
    Keycloak is unreachable, so a failure degrades to what this app has seen
    itself rather than erroring.
    """
    client = get_keycloak_client()
    if client is None:
        return {}, set(), False

    links: dict[str, list[str]] = {}
    stale: set[str] = set()
    try:
        for identity in identities:
            subject = identity.provider_subject
            if not await client.user_exists(subject):
                # A merged-away or deleted realm account. The row survives
                # locally with no way to tell from the outside that nothing can
                # ever authenticate as it again.
                stale.add(subject)
                continue
            links[subject] = [
                alias
                for entry in await client.federated_identities(subject)
                if (alias := entry.get("identityProvider"))
            ]
    except Exception:
        logger.warning("account: Keycloak lookup failed", exc_info=True)
        return {}, set(), False
    return links, stale, True


class AccountAdmin(BaseView):
    name = "Account"
    icon = "fa-solid fa-user-shield"

    @expose("/account", identity="account", methods=["GET"])
    async def account_page(self, request: Request) -> Response:
        user_id = int(request.session.get("user_id") or current_user_id_var.get() or 0)
        settings = get_settings()
        dismissed = set(request.session.get("link_dismissed") or [])

        async with get_session_factory()() as session:
            identities = (
                (
                    await session.execute(
                        select(Identity)
                        .where(Identity.user_id == user_id)
                        .order_by(Identity.linked_at)
                    )
                )
                .scalars()
                .all()
            )
            candidates = [
                candidate
                for candidate in await link_candidates(session, user_id)
                if candidate.id not in dismissed
            ]

        # After the session closes: a Keycloak round trip is no reason to hold
        # a DB connection open.
        links, stale, links_known = await _linked_providers(identities)

        return await self.templates.TemplateResponse(
            request,
            "sqladmin/account.html",
            {
                "identities": identities,
                "candidates": candidates,
                "links": links,
                "stale": stale,
                "links_known": links_known,
                "keycloak_account_url": settings.keycloak_account_url,
                # Which of the rows is the credential in use right now. Two
                # principals for one person is the confusing case this page
                # exists to untangle, and it is the first thing to know.
                "subject": request.session.get("subject"),
                "broker_labels": BROKER_LABELS,
            },
        )
