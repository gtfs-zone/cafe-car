from fastapi import APIRouter, Depends, Form, Header, HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.driver import Driver

router = APIRouter(prefix="/mqtt")


@router.post("/auth")
async def mqtt_auth(
    username: str | None = Form(default=None),
    password: str | None = Form(default=None),
    x_forwarded_for: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> None:
    if x_forwarded_for is not None:
        raise HTTPException(status_code=403)

    if username is None:
        return

    result = await session.exec(select(Driver).where(Driver.username == username))
    driver = result.first()

    if driver is None or password != driver.password:
        raise HTTPException(status_code=403)
