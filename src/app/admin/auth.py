from sqladmin.authentication import AuthenticationBackend
from sqlmodel import select
from starlette.requests import Request

from app.database import get_session_factory
from app.models.user import User
from app.settings import get_settings


class OIDCAuthBackend(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        return True  # Traefik/oauth2-proxy handles login redirect

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        subject = request.headers.get("X-Auth-Request-User") or request.headers.get("X-Forwarded-User")
        x_headers = {k: v for k, v in request.headers.items() if k.lower().startswith("x-")}
        print(f"[AUTH] authenticate called: path={request.url.path!r} subject={subject!r} x_headers={x_headers!r}", flush=True)
        if not subject:
            print("[AUTH] no X-Auth-Request-User header — returning False", flush=True)
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
                if not user:
                    print(f"[AUTH] creating new user subject={subject!r}", flush=True)
                    user = User(
                        provider=settings.oidc_provider,
                        provider_subject=subject,
                        email=request.headers.get("X-Auth-Request-Email"),
                    )
                    session.add(user)
                    await session.commit()
                else:
                    print(f"[AUTH] existing user id={user.id} subject={subject!r}", flush=True)
        except Exception as e:
            print(f"[AUTH] EXCEPTION during DB lookup for subject={subject!r}: {e!r}", flush=True)
            raise
        request.session["subject"] = subject
        return True
