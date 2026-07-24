from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from sqladmin import Admin
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from cafe_car.admin.auth import OIDCAuthBackend
from cafe_car.admin.context import current_subject_var
from cafe_car.admin.entity_router import router as entity_router
from cafe_car.admin.views import (
    FeedAdmin,
    InformedEntityAdmin,
    ServiceAlertAdmin,
    TrackerAdmin,
    TrackerRuleAdmin,
)
from cafe_car.database import get_engine, get_session_factory
from cafe_car.settings import get_settings


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
        current_subject_var.set(request.session.get("subject", ""))
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

    # Register entity router BEFORE Admin, because Admin mounts at "/" which
    # would otherwise swallow all requests before these routes are reached.
    app.include_router(entity_router)

    auth_backend = OIDCAuthBackend(secret_key=settings.session_secret_key)
    templates_dir = str(Path(__file__).parent / "admin" / "templates")
    admin = Admin(
        app,
        engine=get_engine(),
        authentication_backend=auth_backend,
        base_url="/",
        templates_dir=templates_dir,
    )
    admin.add_view(FeedAdmin)
    admin.add_view(TrackerAdmin)
    admin.add_view(TrackerRuleAdmin)
    admin.add_view(ServiceAlertAdmin)
    admin.add_view(InformedEntityAdmin)

    return app


app = create_admin_app()
