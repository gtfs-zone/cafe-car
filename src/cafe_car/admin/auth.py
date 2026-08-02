import base64
import json
import logging

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse

from cafe_car.accounts import resolve_login
from cafe_car.admin.context import current_user_id_var
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


class OIDCAuthBackend(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        return True  # Traefik/oauth2-proxy handles login redirect

    async def logout(self, request: Request) -> RedirectResponse:
        request.session.clear()
        logout_url = get_settings().oauth2_proxy_logout_url
        return RedirectResponse(url=logout_url)

    async def authenticate(self, request: Request) -> bool:
        subject = request.headers.get("X-Auth-Request-User") or request.headers.get(
            "X-Forwarded-User"
        )
        if not subject:
            logger.warning(
                "authenticate: no subject header for path=%s", request.url.path
            )
            return False
        try:
            settings = get_settings()
            # In debug mode oauth2-proxy is configured with
            # PASS_AUTHORIZATION_HEADER, which forwards the ID token
            # (always a JWT with email/name claims).
            # In production the access token is used instead.
            if settings.debug:
                auth_header = request.headers.get("Authorization", "")
                token = (
                    auth_header.removeprefix("Bearer ")
                    if auth_header.startswith("Bearer ")
                    else None
                )
            else:
                token = request.headers.get("X-Auth-Request-Access-Token")
            claims = _decode_jwt_claims(token) if token else {}
            # Only use JWT claims if the token's subject matches to avoid
            # trusting claims from a mismatched or injected token.
            claims = claims if claims.get("sub") == subject else {}
            email = (
                claims.get("email")
                or request.headers.get("X-Auth-Request-Email")
                or None
            )
            # Only the token can vouch for an address being verified — the
            # proxy header carries the address with no such claim attached.
            # Account linking keys off this, so it must not be generous.
            email_verified = bool(claims.get("email_verified") and claims.get("email"))
            display_name = (claims.get("name") or "")[:128].strip() or None

            factory = get_session_factory()
            async with factory() as session:
                user = await resolve_login(
                    session,
                    provider=settings.oidc_provider,
                    subject=subject,
                    email=email,
                    email_verified=email_verified,
                    display_name=display_name,
                )
                # Feeds shared with them before they had an account. Matched on
                # verified addresses only, inside claim_invites.
                await claim_invites(session, user)
        except Exception:
            logger.exception("authenticate: DB error for subject=%s", subject)
            raise
        request.session.clear()
        # Authoritative for this request. SubjectMiddleware primes the var from
        # the session before we get here, which is a request behind: on the
        # first request of a session it is still 0, and on a browser that
        # switches users it still holds the *previous* user — which would feed
        # their feed list into scaffold_form's dropdown. Overwrite it now that
        # the identity is actually known.
        current_user_id_var.set(user.id)
        # user_id is what every scoped query filters on. `subject` is kept for
        # display and debugging only — nothing authorises against it any more.
        request.session["user_id"] = user.id
        request.session["subject"] = subject
        if user.display_name:
            request.session["display_name"] = user.display_name
        if user.primary_email:
            request.session["email"] = user.primary_email
        return True
