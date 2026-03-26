from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request

from core.config import JWT_EXPIRY_MINUTES, JWT_REFRESH_EXPIRY_DAYS


class TokenRefreshMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if hasattr(request.state, "new_tokens"):
            for key, value in request.state.new_tokens.items():
                max_age = (
                    JWT_EXPIRY_MINUTES * 60
                    if key == "auth_token"
                    else JWT_REFRESH_EXPIRY_DAYS * 24 * 60 * 60
                )
                response.set_cookie(
                    key=key,
                    value=value,
                    httponly=True,
                    secure=True,
                    samesite="none",
                    max_age=max_age,
                )
        return response
