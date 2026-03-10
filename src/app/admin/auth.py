import base64
import json
import logging

from sqladmin.authentication import AuthenticationBackend
from sqlmodel import select
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.database import get_session_factory
from railroad_club.models.user import User
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _decode_jwt_claims(token: str) -> dict:
    """Decode JWT payload without verification (token already verified by oauth2-proxy)."""
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
        subject = request.headers.get("X-Auth-Request-User") or request.headers.get("X-Forwarded-User")
        if not subject:
            logger.warning("authenticate: no subject header for path=%s", request.url.path)
            return False
        try:
            settings = get_settings()
            factory = get_session_factory()
            async with factory() as session:
                user = await session.scalar(
                    select(User).where(
                        User.provider == settings.oidc_provider,
                        User.provider_subject == subject,
                    )
                )
                # In debug mode oauth2-proxy is configured with PASS_AUTHORIZATION_HEADER,
                # which forwards the ID token (always a JWT with email/name claims).
                # In production the access token is used instead.
                if settings.debug:
                    auth_header = request.headers.get("Authorization", "")
                    token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
                else:
                    token = request.headers.get("X-Auth-Request-Access-Token")
                claims = _decode_jwt_claims(token) if token else {}
                # Only use JWT claims if the token's subject matches to avoid
                # trusting claims from a mismatched or injected token.
                claims = claims if claims.get("sub") == subject else {}
                email = claims.get("email") or request.headers.get("X-Auth-Request-Email") or None
                display_name = (claims.get("name") or "")[:128].strip() or None
                if not user:
                    logger.info("authenticate: creating new user subject=%s", subject)
                    user = User(
                        provider=settings.oidc_provider,
                        provider_subject=subject,
                        email=email,
                        display_name=display_name,
                    )
                    session.add(user)
                    await session.commit()
                else:
                    dirty = False
                    if email and user.email != email:
                        user.email = email
                        dirty = True
                    if display_name and user.display_name != display_name:
                        user.display_name = display_name
                        dirty = True
                    if dirty:
                        logger.info("authenticate: updated profile for user id=%s", user.id)
                        await session.commit()
        except Exception:
            logger.exception("authenticate: DB error for subject=%s", subject)
            raise
        request.session.clear()
        request.session["subject"] = subject
        if user.display_name:
            request.session["display_name"] = user.display_name
        if user.email:
            request.session["email"] = user.email
        return True
