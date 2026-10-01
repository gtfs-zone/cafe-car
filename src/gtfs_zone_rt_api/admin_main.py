from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from gtfs_zone_rt_api.admin.auth import request_is_admin
from gtfs_zone_rt_api.admin.context import (
    current_user_id_var,
    current_user_is_admin_var,
)
from gtfs_zone_rt_api.admin.entity_router import router as entity_router
from gtfs_zone_rt_api.api import router as api_router
from gtfs_zone_rt_api.database import get_engine, get_session_factory
from gtfs_zone_rt_api.settings import get_settings


class DBSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        async with get_session_factory()() as session:
            request.state.session = session
            return await call_next(request)


class SubjectMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Both vars are set unconditionally, never merely when a value is
        # found, so a request always starts from this request's own answer and
        # can never observe a leftover from an earlier one.
        #
        # The user id is primed from the session and is a request behind, which
        # `authenticate` corrects; admin is read from the token instead, because
        # a cookie is not evidence of group membership.
        current_user_id_var.set(int(request.session.get("user_id") or 0))
        current_user_is_admin_var.set(request_is_admin(request))
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
    app.add_middleware(SubjectMiddleware)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)

    app.include_router(entity_router)
    # rt-manager's JSON API. Shares this app's middleware and therefore its
    # oauth2-proxy headers, its DB session and its identity resolution.
    app.include_router(api_router)

    return app


app = create_admin_app()
