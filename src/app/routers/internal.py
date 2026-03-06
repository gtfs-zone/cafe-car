from fastapi import APIRouter, Depends, Header, HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.feed import Feed

router = APIRouter()


@router.get("/feed_urls")
async def feed_urls(
    x_forwarded_for: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[str]:
    if x_forwarded_for is not None:
        raise HTTPException(status_code=403)

    result = await session.exec(select(Feed.static_feed_url))
    return list(result.all())
