"""Resolving a login, and spotting a person who has two accounts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from railroad_club.models.identity import Identity
from sqlalchemy import select

from cafe_car.accounts import (
    link_candidates,
    resolve_login,
    user_with_verified_email,
)
from tests.factories import PROVIDER, add_identity, make_user

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession


async def test_unknown_credential_creates_a_person(session: AsyncSession) -> None:
    result = await resolve_login(
        session,
        provider=PROVIDER,
        subject="brand-new",
        email="new@example.com",
        email_verified=True,
        display_name="New Person",
    )

    assert result.user.id is not None
    assert result.user.primary_email == "new@example.com"
    assert result.link_candidate_id is None
    identity = await session.scalar(
        select(Identity).where(Identity.provider_subject == "brand-new")
    )
    assert identity.user_id == result.user.id
    assert identity.email_verified is True


async def test_known_credential_returns_the_same_person(
    session: AsyncSession,
) -> None:
    user = await make_user(session, email="me@example.com", subject="stable")

    result = await resolve_login(
        session, provider=PROVIDER, subject="stable", email="me@example.com"
    )

    assert result.user.id == user.id
    assert result.link_candidate_id is None


async def test_profile_is_refreshed_on_sign_in(session: AsyncSession) -> None:
    user = await make_user(
        session, email="old@example.com", subject="stable", display_name="Old Name"
    )

    await resolve_login(
        session,
        provider=PROVIDER,
        subject="stable",
        email="new@example.com",
        email_verified=True,
        display_name="New Name",
    )

    await session.refresh(user)
    assert user.primary_email == "new@example.com"
    assert user.display_name == "New Name"


async def test_changed_address_is_unverified_until_this_login_says_so(
    session: AsyncSession,
) -> None:
    await make_user(session, email="old@example.com", subject="stable", verified=True)

    await resolve_login(
        session,
        provider=PROVIDER,
        subject="stable",
        email="different@example.com",
        email_verified=False,
    )

    identity = await session.scalar(
        select(Identity).where(Identity.provider_subject == "stable")
    )
    assert identity.email == "different@example.com"
    assert identity.email_verified is False


async def test_the_broker_is_recorded_on_a_new_credential(
    session: AsyncSession,
) -> None:
    await resolve_login(
        session,
        provider=PROVIDER,
        subject="fresh",
        email="fresh@example.com",
        email_verified=True,
        broker_alias="github",
    )

    identity = await session.scalar(
        select(Identity).where(Identity.provider_subject == "fresh")
    )
    assert identity.broker_alias == "github"
    assert identity.last_seen_at is not None


async def test_a_login_without_the_claim_does_not_forget_the_broker(
    session: AsyncSession,
) -> None:
    """The issuer not saying which broker was used is not it saying "none"."""
    await resolve_login(
        session,
        provider=PROVIDER,
        subject="stable",
        email="me@example.com",
        email_verified=True,
        broker_alias="google",
    )

    await resolve_login(
        session,
        provider=PROVIDER,
        subject="stable",
        email="me@example.com",
        email_verified=True,
    )

    identity = await session.scalar(
        select(Identity).where(Identity.provider_subject == "stable")
    )
    assert identity.broker_alias == "google"


async def test_a_login_via_a_different_broker_updates_the_recorded_broker(
    session: AsyncSession,
) -> None:
    """Signing in via a second linked provider is current, positive
    information; it must replace the stale broker from an earlier login,
    not be silently discarded in its favor."""
    await resolve_login(
        session,
        provider=PROVIDER,
        subject="stable",
        email="me@example.com",
        email_verified=True,
        broker_alias="github",
    )

    await resolve_login(
        session,
        provider=PROVIDER,
        subject="stable",
        email="me@example.com",
        email_verified=True,
        broker_alias="google",
    )

    identity = await session.scalar(
        select(Identity).where(Identity.provider_subject == "stable")
    )
    assert identity.broker_alias == "google"


async def test_last_seen_is_stamped_on_a_returning_credential(
    session: AsyncSession,
) -> None:
    user = await make_user(session, email="me@example.com", subject="stable")
    identity = await session.scalar(
        select(Identity).where(Identity.provider_subject == "stable")
    )
    assert identity.last_seen_at is None  # the factory has never signed in

    await resolve_login(
        session, provider=PROVIDER, subject="stable", email="me@example.com"
    )

    await session.refresh(identity)
    assert identity.last_seen_at is not None
    assert identity.user_id == user.id


async def test_a_verified_email_collision_suggests_a_link_but_does_not_merge(
    session: AsyncSession,
) -> None:
    existing = await make_user(session, email="both@example.com", verified=True)

    result = await resolve_login(
        session,
        provider=PROVIDER,
        subject="second-account",
        email="both@example.com",
        email_verified=True,
    )

    assert result.link_candidate_id == existing.id
    assert result.user.id != existing.id, "never merge without confirmation"


async def test_an_unverified_email_collision_suggests_nothing(
    session: AsyncSession,
) -> None:
    await make_user(session, email="both@example.com", verified=True)

    result = await resolve_login(
        session,
        provider=PROVIDER,
        subject="second-account",
        email="both@example.com",
        email_verified=False,
    )

    assert result.link_candidate_id is None


async def test_collision_against_an_unverified_holder_suggests_nothing(
    session: AsyncSession,
) -> None:
    """The *other* side has to be proven too, or a squatter's unverified row
    would pull an incoming login into their account."""
    await make_user(session, email="both@example.com", verified=False)

    result = await resolve_login(
        session,
        provider=PROVIDER,
        subject="second-account",
        email="both@example.com",
        email_verified=True,
    )

    assert result.link_candidate_id is None


class TestUserWithVerifiedEmail:
    async def test_matches_case_insensitively(self, session: AsyncSession) -> None:
        user = await make_user(session, email="Mixed.Case@Example.com")
        found = await user_with_verified_email(session, "mixed.case@example.com")
        assert found is not None
        assert found.id == user.id

    async def test_ignores_unverified(self, session: AsyncSession) -> None:
        await make_user(session, email="unproven@example.com", verified=False)
        assert await user_with_verified_email(session, "unproven@example.com") is None

    async def test_excludes_the_caller(self, session: AsyncSession) -> None:
        user = await make_user(session, email="mine@example.com")
        found = await user_with_verified_email(
            session, "mine@example.com", exclude_user_id=user.id
        )
        assert found is None


class TestLinkCandidates:
    async def test_finds_the_other_holder_of_a_verified_address(
        self, session: AsyncSession
    ) -> None:
        first = await make_user(session, email="shared@example.com")
        second = await make_user(session, email="shared@example.com")

        assert [u.id for u in await link_candidates(session, second.id)] == [first.id]

    async def test_never_suggests_the_user_themselves(
        self, session: AsyncSession
    ) -> None:
        user = await make_user(session, email="only@example.com")
        await add_identity(session, user, email="only@example.com")

        assert await link_candidates(session, user.id) == []

    async def test_ignores_unverified_addresses_on_either_side(
        self, session: AsyncSession
    ) -> None:
        await make_user(session, email="shared@example.com", verified=False)
        mine = await make_user(session, email="shared@example.com", verified=False)

        assert await link_candidates(session, mine.id) == []

    async def test_deduplicates_a_person_reachable_by_two_addresses(
        self, session: AsyncSession
    ) -> None:
        other = await make_user(session, email="one@example.com")
        await add_identity(session, other, email="two@example.com")
        mine = await make_user(session, email="one@example.com")
        await add_identity(session, mine, email="two@example.com")

        assert [u.id for u in await link_candidates(session, mine.id)] == [other.id]
