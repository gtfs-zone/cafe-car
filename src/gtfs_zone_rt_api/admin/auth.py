import base64
import json
import logging

from gtfs_zone_db_models.models.identity import Identity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from gtfs_zone_rt_api.accounts import resolve_login
from gtfs_zone_rt_api.admin.context import (
    current_user_id_var,
    current_user_is_admin_var,
)
from gtfs_zone_rt_api.settings import get_settings
from gtfs_zone_rt_api.sharing import claim_invites

log = logging.getLogger(__name__)


def _decode_jwt_claims(token: str) -> dict:
    """Decode JWT payload without verification (already verified by oauth2-proxy)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # fix padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def request_subject(request: Request) -> str | None:
    """Who oauth2-proxy says is calling.

    Traefik's ForwardAuth overwrites these headers from the proxy's response, so
    a client cannot set them.
    """
    return request.headers.get("X-Auth-Request-User") or request.headers.get(
        "X-Forwarded-User"
    )


def _request_token(request: Request) -> str | None:
    # In debug mode oauth2-proxy is configured with PASS_AUTHORIZATION_HEADER,
    # which forwards the ID token (always a JWT with email/name claims). In
    # production the access token is used instead.
    if get_settings().debug:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header.removeprefix("Bearer ")
        return None
    return request.headers.get("X-Auth-Request-Access-Token")


def verified_claims(request: Request) -> dict:
    """Token claims, but only when the token vouches for the calling subject.

    A token whose `sub` does not match the proxy header is discarded whole, so
    a mismatched or injected token cannot contribute a single claim.
    """
    subject = request_subject(request)
    if not subject:
        return {}
    token = _request_token(request)
    claims = _decode_jwt_claims(token) if token else {}
    return claims if claims.get("sub") == subject else {}


def claims_are_admin(claims: dict) -> bool:
    """Membership of the Keycloak admin group, from the flat `groups` claim.

    Callers must pass claims that already went through `verified_claims`.
    Anything unexpected in the claim (absent, null, a bare string) is not admin.
    """
    groups = claims.get("groups")
    return get_settings().admin_group in groups if isinstance(groups, list) else False


def request_is_admin(request: Request) -> bool:
    """Whether this caller is an admin, answered from the token every time.

    The hand-written routes in `entity_router` come through here, so "who is an
    admin" has one definition. It does not consult the session: a cookie is not
    evidence of group membership, and a route may see a stale one or none at all.
    """
    return claims_are_admin(verified_claims(request))


async def resolve_request_user_id(request: Request, session: AsyncSession) -> int:
    """Who is calling, for routes that SQLAdmin's `authenticate` never ran for.

    The hand-written routes (`admin/entity_router.py`, `gtfs_zone_rt_api/api`) sit
    outside SQLAdmin, so nothing has resolved an identity for them and the
    session cookie may be stale or belong to whoever used this browser last.
    The proxy header is the authority: the cached session id is used only when
    it agrees with the header, and otherwise the identity is looked up afresh.
    Returns 0 (which matches no rows anywhere) rather than falling back to the
    cookie.

    Sets `current_user_is_admin_var` as a side effect, from the token on every
    path and never from the session: a cookie is not evidence of group
    membership. `request_is_admin` applies the same subject-match guard the
    header does, so the identity and the admin flag rest on the same evidence.
    """
    subject = request_subject(request)
    if not subject:
        current_user_is_admin_var.set(False)
        return 0
    current_user_is_admin_var.set(request_is_admin(request))
    cached = request.session.get("user_id")
    if cached and request.session.get("subject") == subject:
        return int(cached)
    user_id = await session.scalar(
        select(Identity.user_id).where(
            Identity.provider == get_settings().oidc_provider,
            Identity.provider_subject == subject,
        )
    )
    return int(user_id or 0)


async def ensure_identity(request: Request, session: AsyncSession) -> int:
    """Resolve the caller's ``User``, creating it and claiming invites on first
    sight, and prime the session the way ``resolve_request_user_id`` expects.

    This is the provisioning path: it writes a new ``User``/``Identity`` row
    the first time a credential is seen and claims invites waiting on its
    verified email. ``resolve_request_user_id`` is the fast, read-only lookup
    that runs on every request; a caller reaches here only when that lookup
    found nothing, so this can afford to be heavier. Returns 0, the same as
    ``resolve_request_user_id``, when there is no subject header to resolve.
    """
    subject = request_subject(request)
    if not subject:
        return 0
    settings = get_settings()
    claims = verified_claims(request)
    is_admin = claims_are_admin(claims)
    email = claims.get("email") or request.headers.get("X-Auth-Request-Email") or None
    # Only the token can vouch for an address being verified; the proxy header
    # carries the address with no such claim attached. Account linking keys
    # off this, so it must not be generous.
    email_verified = bool(claims.get("email_verified") and claims.get("email"))
    display_name = (claims.get("name") or "")[:128].strip() or None
    # Which upstream provider Keycloak brokered this session through, from a
    # user-session-note mapper on the client. A direct realm login has no such
    # note, so the claim is simply absent. Display only; `provider_subject` is
    # still what identifies the caller.
    broker_alias = (claims.get("identity_provider") or "")[:64].strip() or None

    user, link_candidate_id = await resolve_login(
        session,
        provider=settings.oidc_provider,
        subject=subject,
        email=email,
        email_verified=email_verified,
        display_name=display_name,
        broker_alias=broker_alias,
    )
    if link_candidate_id is not None:
        # Two principals, one human. Keycloak's first-broker-login flow
        # normally links these upstream, so reaching here means something
        # bypassed it. /account offers the merge; this line is so the
        # duplicate is visible in the log even if they never take it up.
        log.warning(
            "ensure_identity: new user=%s duplicates user=%s on %s",
            user.id,
            link_candidate_id,
            email,
        )
    # Feeds shared with them before they had an account. Matched on verified
    # addresses only, inside claim_invites.
    await claim_invites(session, user)

    prior = dict(request.session)
    request.session.clear()
    # Authoritative for this request. SubjectMiddleware primes the var from
    # the session before we get here, which is a request behind: on the first
    # request of a session it is still 0, and on a browser that switches users
    # it still holds the *previous* user. Overwrite it now that the identity
    # is actually known.
    current_user_id_var.set(user.id)
    current_user_is_admin_var.set(is_admin)
    # user_id is what every scoped query filters on. `subject` is kept for
    # display and debugging only; nothing authorises against it any more.
    request.session["user_id"] = user.id
    request.session["subject"] = subject
    if user.display_name:
        request.session["display_name"] = user.display_name
    if user.primary_email:
        request.session["email"] = user.primary_email
    # Carry dismissals across the clear above, but only for the same person; a
    # browser that switched users must not inherit the previous one's "don't
    # ask me again".
    if prior.get("user_id") == user.id and prior.get("link_dismissed"):
        request.session["link_dismissed"] = prior["link_dismissed"]
    return user.id
