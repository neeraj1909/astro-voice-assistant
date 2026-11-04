"""Shared server-to-server authentication dependency for local API routes."""

import hmac

from fastapi import HTTPException, Request

MIN_GATEWAY_TOKEN_LENGTH = 32


def gateway_token_is_configured(token: str | None) -> bool:
    return bool(
        isinstance(token, str)
        and token.isascii()
        and len(token) >= MIN_GATEWAY_TOKEN_LENGTH
        and not any(character.isspace() for character in token)
    )


async def require_gateway_token(request: Request) -> None:
    expected = getattr(request.app.state, "gateway_token", None)
    if not gateway_token_is_configured(expected):
        raise HTTPException(status_code=503, detail="Chat gateway is not configured")

    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not supplied
        or any(character.isspace() for character in supplied)
        or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("ascii"))
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
