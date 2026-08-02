from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from sqladmin import Admin
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cafe_car.admin.auth import OIDCAuthBackend
from cafe_car.admin.context import current_user_id_var
from cafe_car.admin.entity_router import router as entity_router
from cafe_car.admin.links import editor_url, viz_url
from cafe_car.admin.views import (
    FeedAdmin,
    InformedEntityAdmin,
    ServiceAlertAdmin,
    TrackerAdmin,
    TrackerRuleAdmin,
)
from cafe_car.database import get_engine, get_session_factory
from cafe_car.settings import get_settings

# SQLAdmin's static assets (vendored Tabler/Bootstrap CSS+JS). These are
# public, immutable-per-deploy files: skip session/DB middleware for them and
# make them cacheable so browsers don't refetch every stylesheet on each page
# load (Starlette's StaticFiles sends no Cache-Control, which causes a flash
# of unstyled content when the assets are slow).
STATICS_PREFIX = "/statics/"

STATICS_MAX_AGE = 60 * 60 * 24  # 1 day; ETag revalidation still applies after


class StaticsCacheControlMiddleware:
    """Pure-ASGI middleware that adds Cache-Control to /statics responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(STATICS_PREFIX):
            await self.app(scope, receive, send)
            return

        async def send_with_cache_control(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = f"public, max-age={STATICS_MAX_AGE}"
            await send(message)

        await self.app(scope, receive, send_with_cache_control)


class DBSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path.startswith(STATICS_PREFIX):
            return await call_next(request)
        async with get_session_factory()() as session:
            request.state.session = session
            return await call_next(request)


class SubjectMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path.startswith(STATICS_PREFIX):
            return await call_next(request)
        current_user_id_var.set(int(request.session.get("user_id") or 0))
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

    app.add_middleware(StaticsCacheControlMiddleware)
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

    # The scoped views raise PermissionError when a row is not the caller's.
    # Without this it escapes as a 500; once feeds are shared, non-owners hit
    # the owner-only paths legitimately and deserve a real answer.
    async def access_denied(request: Request, exc: Exception) -> Response:
        return await admin.templates.TemplateResponse(
            request,
            "sqladmin/error.html",
            {"status_code": 403, "message": str(exc) or "Access denied"},
            status_code=403,
        )

    admin.admin.add_exception_handler(PermissionError, access_denied)

    # Expose cross-app deep-link helpers to the feed detail template.
    admin.templates.env.globals["viz_url"] = viz_url
    admin.templates.env.globals["editor_url"] = editor_url

    return app


app = create_admin_app()
