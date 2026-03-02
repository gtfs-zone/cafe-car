from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from sqladmin import Admin
from starlette.middleware.sessions import SessionMiddleware

from app.admin.auth import OIDCAuthBackend
from app.admin.views import DriverAdmin, FeedAdmin
from app.database import get_engine
from app.settings import get_settings


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

    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)

    auth_backend = OIDCAuthBackend(secret_key=settings.session_secret_key)
    admin = Admin(app, engine=get_engine(), authentication_backend=auth_backend)
    admin.add_view(FeedAdmin)
    admin.add_view(DriverAdmin)

    return app


app = create_admin_app()
