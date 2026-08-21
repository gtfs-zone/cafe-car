"""yard-master's authenticated JSON API.

Mounted at ``/api`` on the **admin** app, so it inherits ``SubjectMiddleware``,
``DBSessionMiddleware`` and the oauth2-proxy headers Traefik's ForwardAuth puts
in front of that app. Same host as the SPA on purpose: same origin means no
CORS, no credentialed preflight, and the ``X-Auth-Request-*`` headers arrive
untouched.

It must be registered *before* ``Admin`` mounts, for the same reason
``entity_router`` already is: the mount at ``/`` swallows anything registered
after it.

``require_csrf`` is a dependency of the whole router rather than of individual
routes, so a mutation added in a later phase cannot forget it.
"""

from fastapi import APIRouter, Depends

from cafe_car.api import (
    alerts,
    events,
    feeds,
    members,
    positions,
    trackers,
    uploads,
)
from cafe_car.api.deps import require_csrf

router = APIRouter(prefix="/api", dependencies=[Depends(require_csrf)])
router.include_router(feeds.router)
router.include_router(trackers.router)
router.include_router(alerts.router)
router.include_router(members.router)
router.include_router(events.router)
router.include_router(positions.router)
router.include_router(uploads.router)

__all__ = ["router"]
