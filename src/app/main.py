from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from sqladmin import Admin
from starlette.middleware.sessions import SessionMiddleware

from app.admin.auth import AutheliaAuthBackend
from app.admin.views import DriverAdmin, FeedAdmin
from app.database import get_engine
from app.routers.gtfs_rt import router as gtfs_rt_router
from app.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    # Verify Redis connectivity on startup
    redis = aioredis.from_url(str(settings.redis_url))
    await redis.ping()
    app.state.redis = redis

    yield

    await redis.aclose()
    await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="redis-gtfs-rt-api",
        lifespan=lifespan,
    )

    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)

    # GTFS-RT stub endpoints
    app.include_router(gtfs_rt_router)

    # SQLAdmin
    auth_backend = AutheliaAuthBackend(secret_key=settings.session_secret_key)
    admin = Admin(app, engine=get_engine(), authentication_backend=auth_backend)
    admin.add_view(FeedAdmin)
    admin.add_view(DriverAdmin)

    return app


app = create_app()
