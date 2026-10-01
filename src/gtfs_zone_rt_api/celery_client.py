from celery import Celery

from gtfs_zone_rt_api.settings import get_settings


def _make_celery() -> Celery:
    settings = get_settings()
    return Celery(broker=settings.celery_broker_url)


celery_app = _make_celery()
