import base64
import json
import logging

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse

from cafe_car.accounts import resolve_login
from cafe_car.admin.context import current_user_id_var, current_user_is_admin_var
from cafe_car.database import get_session_factory
from cafe_car.settings import get_settings
from cafe_car.sharing import claim_invites

logger = logging.getLogger(__name__)


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

    Both the SQLAdmin views (through `OIDCAuthBackend.authenticate`) and the
    hand-written routes in `entity_router` come through here, so "who is an
    admin" has one definition and the two cannot drift. In particular it does
    not consult the session: a cookie is not evidence of group membership, and
    routes outside SQLAdmin may see a stale one or none at all.
    """
    return claims_are_admin(verified_claims(request))


class OIDCAuthBackend(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        return True  # Traefik/oauth2-proxy handles login redirect

    async def logout(self, request: Request) -> RedirectResponse:
        request.session.clear()
        logout_url = get_settings().oauth2_proxy_logout_url
        return RedirectResponse(url=logout_url)

    async def authenticate(self, request: Request) -> bool:
        subject = request_subject(request)
        if not subject:
            logger.warning(
                "authenticate: no subject header for path=%s", request.url.path
            )
            return False
        try:
            settings = get_settings()
            claims = verified_claims(request)
            is_admin = claims_are_admin(claims)
            email = (
                claims.get("email")
                or request.headers.get("X-Auth-Request-Email")
                or None
            )
            # Only the token can vouch for an address being verified; the
            # proxy header carries the address with no such claim attached.
            # Account linking keys off this, so it must not be generous.
            email_verified = bool(claims.get("email_verified") and claims.get("email"))
            display_name = (claims.get("name") or "")[:128].strip() or None
            # Which upstream provider Keycloak brokered this session through,
            # from a user-session-note mapper on the client. A direct realm
            # login has no such note, so the claim is simply absent. Display
            # only; `provider_subject` is still what identifies the caller.
            broker_alias = (claims.get("identity_provider") or "")[:64].strip() or None

            factory = get_session_factory()
            async with factory() as session:
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
                    # Two principals, one human. Keycloak's first-broker-login
                    # flow normally links these upstream, so reaching here means
                    # something bypassed it. /account offers the merge; this
                    # line is so the duplicate is visible in the log even if
                    # they never take it up.
                    logger.warning(
                        "authenticate: new user=%s duplicates user=%s on %s",
                        user.id,
                        link_candidate_id,
                        email,
                    )
                # Feeds shared with them before they had an account. Matched on
                # verified addresses only, inside claim_invites.
                await claim_invites(session, user)
        except Exception:
            logger.exception("authenticate: DB error for subject=%s", subject)
            raise
        prior = dict(request.session)
        request.session.clear()
        # Authoritative for this request. SubjectMiddleware primes the var from
        # the session before we get here, which is a request behind: on the
        # first request of a session it is still 0, and on a browser that
        # switches users it still holds the *previous* user, which would feed
        # their feed list into scaffold_form's dropdown. Overwrite it now that
        # the identity is actually known.
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
        # Carry dismissals across the clear above, but only for the same
        # person; a browser that switched users must not inherit the previous
        # one's "don't ask me again".
        if prior.get("user_id") == user.id and prior.get("link_dismissed"):
            request.session["link_dismissed"] = prior["link_dismissed"]
        return True
