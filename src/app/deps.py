from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models.user import User


async def get_current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    username = request.headers.get("Remote-User")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")

    result = await session.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            username=username,
            email=request.headers.get("Remote-Email"),
            display_name=request.headers.get("Remote-Name"),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    else:
        # Update profile fields if they changed
        email = request.headers.get("Remote-Email")
        display_name = request.headers.get("Remote-Name")
        if user.email != email or user.display_name != display_name:
            user.email = email
            user.display_name = display_name
            session.add(user)
            await session.commit()
            await session.refresh(user)

    return user


# Re-export get_session for use in routers
async def get_db_session() -> AsyncGenerator[AsyncSession]:
    async for session in get_session():
        yield session
