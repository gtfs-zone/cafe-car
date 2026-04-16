from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, HTTPException
from railroad_club.models.feed import Feed
from sqlmodel import select

from cafe_car.database import get_session

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

    result = await session.exec(select(Feed.static_feed_url))
    return list(result.all())
