"""Thin same-origin gateway to the existing chat API."""

import os

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .local_asr import router as local_asr_router
from .security import require_gateway_token


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("user_id", "session_id", "message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ChatResponse(BaseModel):
    request_id: str
    message_id: int
    response_id: int
    user_id: str
    session_id: str
    response: str
    context_used: list[str]


def create_app(
    upstream_url: str | None = None,
    *,
    timeout_seconds: float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    gateway_token: str | None = None,
) -> FastAPI:
    """Build the gateway; upstream configuration is kept on the server."""
    url = upstream_url if upstream_url is not None else os.getenv("CHAT_API_URL", "")
    configured_token = (
        gateway_token if gateway_token is not None else os.getenv("ASTRO_GATEWAY_TOKEN", "")
    )
    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else float(os.getenv("CHAT_API_TIMEOUT_SECONDS", "30"))
    )
    if timeout <= 0:
        raise ValueError("CHAT_API_TIMEOUT_SECONDS must be positive")

    app = FastAPI(title="Astrology Chat Gateway")
    app.state.gateway_token = configured_token
    app.include_router(local_asr_router)

    @app.post(
        "/api/chat",
        response_model=ChatResponse,
        dependencies=[Depends(require_gateway_token)],
    )
    async def chat(payload: ChatRequest) -> ChatResponse:
        if not url:
            raise HTTPException(status_code=503, detail="Chat service is not configured")

        try:
            async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
                upstream = await client.post(url, json=payload.model_dump())
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="Chat service timed out") from exc
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail="Chat service is unavailable") from exc

        if upstream.status_code == 422:
            raise HTTPException(status_code=422, detail="Chat request was rejected")
        if upstream.status_code == 503:
            raise HTTPException(status_code=503, detail="Chat service is unavailable")
        if not upstream.is_success:
            raise HTTPException(status_code=502, detail="Chat service is unavailable")

        try:
            result = ChatResponse.model_validate(upstream.json())
        except ValueError as exc:
            raise HTTPException(
                status_code=502, detail="Chat service returned an invalid response"
            ) from exc

        # Prevent accidental cross-account/session response mixing. This is not authentication.
        if result.user_id != payload.user_id or result.session_id != payload.session_id:
            raise HTTPException(status_code=502, detail="Chat service returned an invalid response")
        return result

    return app


app = create_app()
