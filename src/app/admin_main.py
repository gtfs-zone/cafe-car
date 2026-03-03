import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from sqladmin import Admin
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.admin.auth import OIDCAuthBackend
from app.admin.views import DriverAdmin, FeedAdmin
from app.database import get_engine, get_session_factory
from app.settings import get_settings


class DBSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        async with get_session_factory()() as session:
            request.state.session = session
            return await call_next(request)


class DevAuthMiddleware(BaseHTTPMiddleware):
    """Injects fake oauth2-proxy headers for local development (DEV_AUTH_USER set)."""

    def __init__(self, app, user: str, email: str) -> None:
        super().__init__(app)
        self.user = user.encode()
        self.email = email.encode()

    async def dispatch(self, request: Request, call_next):
        request.scope["headers"] = [
            *request.scope["headers"],
            (b"x-auth-request-user", self.user),
            (b"x-auth-request-email", self.email),
        ]
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    redis = aioredis.from_url(str(settings.redis_url))
    await redis.ping()
    app.state.redis = redis

    yield

    await redis.aclose()
    await get_engine().dispose()


def create_admin_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="redis-gtfs-rt-admin",
        lifespan=lifespan,
    )

    app.add_middleware(DBSessionMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)

    dev_user = os.getenv("DEV_AUTH_USER")
    if dev_user:
        dev_email = os.getenv("DEV_AUTH_EMAIL", f"{dev_user}@local")
        app.add_middleware(DevAuthMiddleware, user=dev_user, email=dev_email)

    auth_backend = OIDCAuthBackend(secret_key=settings.session_secret_key)
    admin = Admin(app, engine=get_engine(), authentication_backend=auth_backend, base_url="/")
    admin.add_view(FeedAdmin)
    admin.add_view(DriverAdmin)

    return app


app = create_admin_app()
