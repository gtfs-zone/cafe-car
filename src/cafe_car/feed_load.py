"""Asking schedule-foamer to (re)download a feed's static zip.

One definition of the task name and one definition of what happens when the
worker is unreachable. Every caller - the admin's create hook, the admin's
reload button and the API's create/reload routes - comes through here, so a
renamed task breaks in one place rather than four.

A dispatch failure is swallowed on purpose. The feed row is already committed
and ``next_retry_at`` will bring it round again, so a broker that is down must
not turn a successful write into a 500.
"""

from __future__ import annotations

import contextlib

LOAD_TASK = "schedule_foamer.tasks.load_feed"


def request_feed_load(feed_id: int) -> None:
    """Queue a static-feed load. Never raises."""
    with contextlib.suppress(Exception):
        from cafe_car.celery_client import celery_app

        celery_app.send_task(LOAD_TASK, args=[feed_id])
