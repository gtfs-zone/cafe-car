"""Who may work on a feed: its owner, its members, and its open invites.

Reading is allowed to anyone who can see the feed, matching the SQLAdmin
members panel this replaces: a member needs to know who else is on a feed, and
the answer contains nothing they could not learn by asking the owner. The
mutations are a different question and go through ``OwnedFeed``, so a member
cannot add another member or remove themselves from someone else's feed.

Sharing matches on a **verified** address alone. That rule is an
account-takeover boundary, not a convenience: an address somebody merely typed
into a provider profile must never receive a feed. It lives in ``sharing.py``
and is not restated here, so the API and the login-time claiming path cannot
disagree about it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from gtfs_zone_db_models.models.user import User

from gtfs_zone_rt_api.api.deps import AccessibleFeed, CurrentUser, DBSession, OwnedFeed
from gtfs_zone_rt_api.api.schemas import (
    InviteOut,
    MemberAdd,
    MemberOut,
    PeopleOut,
    ShareOut,
)
from gtfs_zone_rt_api.sharing import (
    list_members,
    list_open_invites,
    remove_member,
    revoke_invite,
    share_feed,
)

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


@router.post("/feeds/{feed_id}/members", status_code=201)
async def add_person(
    payload: MemberAdd, feed: OwnedFeed, user_id: CurrentUser, session: DBSession
) -> ShareOut:
    """Share the feed with an address, as a member now or an invite for later.

    201 either way. Which of the two happened is in `kind`, and the difference
    matters to the person doing it - an invite grants nothing until somebody
    signs in with a verified copy of that address - so `message` says so in
    words the frontend shows as it stands.
    """
    result = await share_feed(session, feed, payload.email, added_by_user_id=user_id)
    return ShareOut(kind=result.kind, message=result.message)


@router.delete("/feeds/{feed_id}/members/{member_user_id}", status_code=204)
async def remove_person(
    member_user_id: int, feed: OwnedFeed, session: DBSession
) -> Response:
    """Take a member off the feed.

    The owner is not a `feed_member` row, so this cannot remove them however it
    is called; ownership moves through `/transfer` alone. Removing somebody who
    is not a member is a no-op rather than a 404: the caller asked for a state,
    and that state already holds.
    """
    if member_user_id == feed.owner_id:
        raise HTTPException(
            status_code=400,
            detail="The owner cannot be removed. Transfer the feed instead.",
        )
    await remove_member(session, feed.id, member_user_id)
    return Response(status_code=204)


@router.delete("/feeds/{feed_id}/invites/{invite_id}", status_code=204)
async def revoke_open_invite(
    invite_id: int, feed: OwnedFeed, session: DBSession
) -> Response:
    """Withdraw an invite that has not been claimed.

    Matched on the feed as well as the id, so an invite id from another feed is
    left alone. A claimed invite is not touched: it is already a membership,
    and that is removed as a member.
    """
    await revoke_invite(session, feed.id, invite_id)
    return Response(status_code=204)
