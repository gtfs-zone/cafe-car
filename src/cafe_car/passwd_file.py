import asyncio
from pathlib import Path

from sqlmodel import select

from cafe_car.database import get_session_factory

PASSWD_FILE = Path("/run/nanomq/passwd")
_lock = asyncio.Lock()


async def regenerate_passwd_file() -> None:
    async with _lock:
        async with get_session_factory()() as session:
            from railroad_club.models.driver import Driver
            drivers = (await session.exec(select(Driver))).all()

        content = '"public": "public"\n' + "".join(
            f'"{d.username}": "{d.password}"\n' for d in drivers
        )
        tmp = PASSWD_FILE.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content)
        tmp.rename(PASSWD_FILE)
