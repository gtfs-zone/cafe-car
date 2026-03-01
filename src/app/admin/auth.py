from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request


class AutheliaAuthBackend(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        # Authelia handles login at the Traefik level; no-op here
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        username = request.headers.get("Remote-User")
        if not username:
            return False
        request.session["user"] = username
        request.session["email"] = request.headers.get("Remote-Email", "")
        request.session["display_name"] = request.headers.get("Remote-Name", "")
        return True
