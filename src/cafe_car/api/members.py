"""Who may work on a feed: its owner, its members, and its open invites.

Reading is allowed to anyone who can see the feed, matching the SQLAdmin
members panel this replaces: a member needs to know who else is on a feed, and
the answer contains nothing they could not learn by asking the owner. The
mutations are a different question and are owner-only; phase 5 adds them behind
``OwnedFeed``.
"""

from __future__ import annotations

from fastapi import APIRouter
from railroad_club.models.user import User

from cafe_car.api.deps import AccessibleFeed, DBSession
from cafe_car.api.schemas import InviteOut, MemberOut, PeopleOut
from cafe_car.sharing import list_members, list_open_invites

router = APIRouter()


@router.get("/feeds/{feed_id}/members")
async def read_people(feed: AccessibleFeed, session: DBSession) -> PeopleOut:
    owner = await session.get(User, feed.owner_id)
    # The owner is a member of the feed in every sense the UI cares about, but
    # is not a `feed_member` row, so they are prepended here rather than the
    # frontend having to reconstruct them from `feed.owner_id`.
    members = [
        MemberOut(
            user_id=feed.owner_id,
            email=owner.primary_email if owner else None,
            display_name=owner.display_name if owner else None,
            is_owner=True,
            added_by_user_id=None,
            created_at=None,
        )
    ]
    for member in await list_members(session, feed.id):
        members.append(
            MemberOut(
                user_id=member.user_id,
                email=member.user.primary_email if member.user else None,
                display_name=member.user.display_name if member.user else None,
                is_owner=False,
                added_by_user_id=member.added_by_user_id,
                created_at=member.created_at,
            )
        )
    invites = [
        InviteOut(
            id=invite.id,
            email=invite.email,
            invited_by_user_id=invite.invited_by_user_id,
            created_at=invite.created_at,
        )
        for invite in await list_open_invites(session, feed.id)
    ]
    return PeopleOut(members=members, invites=invites)
