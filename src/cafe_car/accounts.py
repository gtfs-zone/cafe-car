"""Resolving a login to a person.

An OIDC login arrives as ``(provider, subject)``. That pair identifies an
:class:`Identity`, and the identity points at the :class:`User` who actually
owns things. A person with GitHub and Google linked has two identities and one
user — which is the entire point of the split.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from railroad_club.models.identity import Identity
from railroad_club.models.user import User
from sqlalchemy import select
from sqlalchemy.orm import selectinload

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)


async def resolve_login(
    session: AsyncSession,
    *,
    provider: str,
    subject: str,
    email: str | None = None,
    email_verified: bool = False,
    display_name: str | None = None,
) -> User:
    """Return the :class:`User` for this login, creating one if needed.

    Refreshes the profile fields we were told about, so a changed display name
    or a newly verified email lands in the database on the next sign-in.
    """
    identity = await session.scalar(
        select(Identity)
        .where(Identity.provider == provider, Identity.provider_subject == subject)
        .options(selectinload(Identity.user))
    )

    if identity is not None:
        user = identity.user
        _refresh_profile(
            identity,
            user,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
        )
        await session.commit()
        return user

    # An unseen credential. Phase 6 inserts the "a verified email already
    # belongs to someone — link instead?" check here; until then a new
    # credential always means a new person.
    logger.info("resolve_login: new identity provider=%s subject=%s", provider, subject)
    user = User(primary_email=email, display_name=display_name)
    session.add(user)
    await session.flush()
    session.add(
        Identity(
            user_id=user.id,
            provider=provider,
            provider_subject=subject,
            email=email,
            email_verified=email_verified,
        )
    )
    await session.commit()
    return user


def _refresh_profile(
    identity: Identity,
    user: User,
    *,
    email: str | None,
    email_verified: bool,
    display_name: str | None,
) -> None:
    if email and identity.email != email:
        identity.email = email
        # A changed address is unverified until this login says otherwise.
        identity.email_verified = email_verified
    elif email and email_verified and not identity.email_verified:
        identity.email_verified = True
    if email and user.primary_email != email:
        user.primary_email = email
    if display_name and user.display_name != display_name:
        user.display_name = display_name
