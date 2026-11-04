import json

import httpx
from fastapi.testclient import TestClient

from app.main import create_app

GATEWAY_TOKEN = "test-gateway-token-" + "x" * 32


def post_chat(client, payload, token: str = GATEWAY_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/chat", json=payload, headers=headers)


def test_gateway_does_not_serve_the_legacy_static_frontend() -> None:
    with TestClient(create_app("http://chat.test/chat")) as client:
        response = client.get("/")
    assert response.status_code == 404


def test_chat_requires_valid_server_bearer_token_before_forwarding() -> None:
    forwarded = False

    def handler(_):
        nonlocal forwarded
        forwarded = True
        return httpx.Response(500)

    app = create_app(
        "http://chat.test/chat",
        gateway_token=GATEWAY_TOKEN,
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        missing = post_chat(
            client,
            {"user_id": "u", "session_id": "s", "message": "Hi"},
            token="",
        )
        invalid = post_chat(
            client,
            {"user_id": "u", "session_id": "s", "message": "Hi"},
            token="wrong-gateway-token",
        )
    assert missing.status_code == invalid.status_code == 401
    assert not forwarded
    assert GATEWAY_TOKEN not in missing.text + invalid.text


def test_chat_fails_closed_when_gateway_token_is_unconfigured_or_short(monkeypatch) -> None:
    monkeypatch.delenv("ASTRO_GATEWAY_TOKEN", raising=False)
    payload = {"user_id": "u", "session_id": "s", "message": "Hi"}
    with TestClient(create_app("http://chat.test/chat")) as client:
        missing = post_chat(client, payload)
    with TestClient(create_app("http://chat.test/chat", gateway_token="short")) as client:
        short = post_chat(client, payload)
    assert missing.status_code == short.status_code == 503


def test_chat_forwards_identity_and_returns_contract() -> None:
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "request_id": "req-1",
                "message_id": 2,
                "response_id": 3,
                "user_id": "user-a",
                "session_id": "session-b",
                "response": "Hello back",
                "context_used": [],
            },
        )

    app = create_app(
        "http://chat.test/chat",
        gateway_token=GATEWAY_TOKEN,
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        response = post_chat(
            client,
            {"user_id": "user-a", "session_id": "session-b", "message": "Hello"},
        )

    assert response.status_code == 200
    assert seen == [{"user_id": "user-a", "session_id": "session-b", "message": "Hello"}]
    assert response.json()["response"] == "Hello back"


def test_chat_rejects_blank_fields_before_upstream() -> None:
    app = create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)
    with TestClient(app) as client:
        response = post_chat(client, {"user_id": " ", "session_id": "s", "message": "Hi"})
    assert response.status_code == 422


def test_chat_accepts_4000_character_message() -> None:
    seen: list[str] = []

    def handler(request):
        seen.append(json.loads(request.content)["message"])
        return httpx.Response(
            200,
            json={
                "request_id": "req-1",
                "message_id": 2,
                "response_id": 3,
                "user_id": "u",
                "session_id": "s",
                "response": "OK",
                "context_used": [],
            },
        )

    app = create_app(
        "http://chat.test/chat",
        gateway_token=GATEWAY_TOKEN,
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        response = post_chat(client, {"user_id": "u", "session_id": "s", "message": "x" * 4000})
    assert response.status_code == 200
    assert seen == ["x" * 4000]


def test_chat_rejects_4001_character_message_before_forwarding() -> None:
    seen = False

    def handler(_):
        nonlocal seen
        seen = True
        return httpx.Response(200, json={})

    app = create_app(
        "http://chat.test/chat",
        gateway_token=GATEWAY_TOKEN,
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        response = post_chat(client, {"user_id": "u", "session_id": "s", "message": "x" * 4001})
    assert response.status_code == 422
    assert not seen


def test_upstream_failure_does_not_leak_response_body() -> None:
    app = create_app(
        "http://chat.test/chat",
        gateway_token=GATEWAY_TOKEN,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, text="private database credential detail")
        ),
    )
    with TestClient(app) as client:
        response = post_chat(client, {"user_id": "u", "session_id": "s", "message": "Hi"})
    assert response.status_code == 502
    assert "private database" not in response.text


def test_upstream_timeout_returns_gateway_timeout() -> None:
    def handler(_):
        raise httpx.ReadTimeout("private upstream diagnostic")

    app = create_app(
        "http://chat.test/chat",
        gateway_token=GATEWAY_TOKEN,
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        response = post_chat(client, {"user_id": "u", "session_id": "s", "message": "Hi"})
    assert response.status_code == 504
    assert "private upstream diagnostic" not in response.text


def test_upstream_cannot_return_another_users_session() -> None:
    body = {
        "request_id": "req-1",
        "message_id": 2,
        "response_id": 3,
        "user_id": "somebody-else",
        "session_id": "s",
        "response": "private response",
        "context_used": [],
    }
    app = create_app(
        "http://chat.test/chat",
        gateway_token=GATEWAY_TOKEN,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    with TestClient(app) as client:
        response = post_chat(client, {"user_id": "u", "session_id": "s", "message": "Hi"})
    assert response.status_code == 502
    assert "private response" not in response.text
