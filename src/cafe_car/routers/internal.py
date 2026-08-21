from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, HTTPException
from railroad_club.models.feed import Feed
from sqlmodel import select

from cafe_car.database import get_session
from cafe_car.feed_urls import feed_static_url

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter()


@router.get("/feed_urls")
async def feed_urls(
    x_forwarded_for: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[str]:
    if x_forwarded_for is not None:
        raise HTTPException(status_code=403)

    # Whole rows rather than the one column: a hosted feed's schedule URL is
    # derived, and a feed with no URL at all contributes nothing here.
    feeds = (await session.exec(select(Feed))).all()
    return [url for feed in feeds if (url := feed_static_url(feed))]
