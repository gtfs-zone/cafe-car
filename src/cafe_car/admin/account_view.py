"""The signed-in person's own account page.

A :class:`BaseView` rather than a route on ``entity_router`` so it renders
inside the admin chrome and picks up a nav entry — and so ``@expose``'s
``login_required`` runs the auth backend for it. The two mutating routes it
posts to live in ``entity_router`` alongside the other permission-checked
actions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from railroad_club.models.identity import Identity
from sqladmin import BaseView, expose
from sqlalchemy import select

from cafe_car.accounts import link_candidates
from cafe_car.admin.context import current_user_id_var
from cafe_car.database import get_session_factory
from cafe_car.settings import get_settings

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


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

        return await self.templates.TemplateResponse(
            request,
            "sqladmin/account.html",
            {
                "identities": identities,
                "candidates": candidates,
                "keycloak_account_url": settings.keycloak_account_url,
            },
        )
