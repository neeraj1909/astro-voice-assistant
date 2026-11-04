from __future__ import annotations

from pathlib import Path
import io
import sys
import types
import wave

import httpx
import pytest
from streamlit.testing.v1 import AppTest
from streamlit_app import GatewayError, post_chat, post_transcription, validate_recording

APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


def test_auth_gate_shows_local_account_forms_before_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_ACCOUNTS_DB", str(tmp_path / "accounts.sqlite3"))
    app = AppTest.from_file(APP_PATH).run()

    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["Sign in", "Create local account"]
    assert len(app.text_area) == 0
    assert any("local demo account" in item.value for item in app.caption)


def test_local_account_registration_gates_chat_and_uses_server_account_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_ACCOUNTS_DB", str(tmp_path / "accounts.sqlite3"))
    app = AppTest.from_file(APP_PATH).run()
    app.text_input(key="register_username").set_value("demo-user")
    app.text_input(key="register_password").set_value("at-least-12-characters")
    app.button(key="register_submit").click().run()

    assert not app.exception
    assert app.session_state["account_id"]
    assert app.session_state["user_id"] == app.session_state["account_id"]
    assert app.session_state["username"] == "demo-user"
    assert app.session_state["register_password"] == ""
    assert len(app.text_area) == 1


def test_chat_posts_account_identity_and_reset_rotates_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_ACCOUNTS_DB", str(tmp_path / "accounts.sqlite3"))
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "test-only-token-with-at-least-32-chars")
    posted: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        posted.append((url, kwargs))
        return httpx.Response(200, json={"response": "A controlled reply."})

    monkeypatch.setattr(httpx, "post", fake_post)
    app = AppTest.from_file(APP_PATH).run()
    app.session_state["account_id"] = "acct-17"
    app.session_state["username"] = "demo-user"
    app.session_state["user_id"] = "acct-17"
    app.session_state["session_id"] = "session-before"
    app.session_state["messages"] = []
    app.session_state["draft_generation"] = 0
    app.run()

    app.text_area(key="draft_0").set_value("What should I reflect on today?").run()
    app.button(key="send_message").click().run()

    assert posted[0][0] == "http://127.0.0.1:8000/api/chat"
    assert posted[0][1]["json"] == {
        "user_id": "acct-17",
        "session_id": "session-before",
        "message": "What should I reflect on today?",
    }
    assert (
        posted[0][1]["headers"]["Authorization"] == "Bearer test-only-token-with-at-least-32-chars"
    )
    assert any("A controlled reply." in item.value for item in app.markdown)
    assert app.text_area(key="draft_1").value == ""

    app.session_state["messages"] = [{"role": "user", "content": "old turn"}]
    app.session_state["session_id"] = "session-before"
    app.run()
    app.button(key="new_conversation").click().run()
    assert app.session_state["session_id"] != "session-before"
    assert app.session_state["messages"] == []


def test_audio_requires_prior_consent_and_explicit_transcribe_action(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_ACCOUNTS_DB", str(tmp_path / "accounts.sqlite3"))
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "test-only-token-with-at-least-32-chars")
    calls: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"text": "hello"})

    monkeypatch.setattr(httpx, "post", fake_post)
    app = AppTest.from_file(APP_PATH).run()
    app.session_state["account_id"] = "acct-17"
    app.session_state["username"] = "demo-user"
    app.session_state["user_id"] = "acct-17"
    app.session_state["session_id"] = "session-a"
    app.run()

    assert len(app.audio_input) == 0
    assert any("English-only" in item.value for item in app.caption)

    consent = app.checkbox(key="audio_consent_0")
    consent.set_value(True).run()
    assert len(app.audio_input) == 1
    assert calls == []

    app.button(key="transcribe_0").click().run()
    assert calls == []
    assert any("Record a voice clip" in item.value for item in app.warning)


def test_recording_validator_accepts_only_bounded_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    validate_recording(output.getvalue())

    with pytest.raises(GatewayError, match="mono 16 kHz"):
        invalid = io.BytesIO()
        with wave.open(invalid, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 2 * 16000)
        validate_recording(invalid.getvalue())


def test_gateway_client_requires_server_token_and_never_leaks_it_to_chat_payload(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"response": "reply"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.delenv("ASTRO_GATEWAY_TOKEN", raising=False)
    with pytest.raises(GatewayError, match="not configured"):
        post_chat("acct", "session", "hello")
    assert calls == []

    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "server-only-token-with-at-least-32-chars")
    assert post_chat("acct", "session", "hello") == "reply"
    assert (
        calls[0][1]["headers"]["Authorization"] == "Bearer server-only-token-with-at-least-32-chars"
    )
    assert "server-only-token-with-at-least-32-chars" not in calls[0][1]["json"].values()


def test_gateway_client_rejects_short_whitespace_and_non_ascii_tokens(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *_args, **_kwargs: pytest.fail("invalid token must not send")
    )
    for invalid_token in ("x" * 31, "x" * 32 + " ", "é" * 32):
        monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", invalid_token)
        with pytest.raises(GatewayError, match="not configured"):
            post_chat("acct", "session", "hello")


@pytest.mark.parametrize(
    "gateway_url",
    [
        "http://example.com:8000",
        "http://192.168.1.2:8000",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com/?next=evil",
        "file:///tmp/gateway",
    ],
)
def test_gateway_client_rejects_unsafe_urls_before_sending_token(monkeypatch, gateway_url):
    monkeypatch.setenv("GATEWAY_URL", gateway_url)
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "server-only-token-with-at-least-32-chars")
    monkeypatch.setattr(
        httpx, "post", lambda *_args, **_kwargs: pytest.fail("unsafe gateway URL must not send")
    )

    with pytest.raises(GatewayError, match="GATEWAY_URL"):
        post_chat("acct", "session", "hello")


@pytest.mark.parametrize(
    "gateway_url", ["http://127.0.0.1:8000", "http://localhost:8000", "https://gateway.example.com"]
)
def test_gateway_client_uses_validated_origin_without_following_redirects(monkeypatch, gateway_url):
    monkeypatch.setenv("GATEWAY_URL", gateway_url)
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "server-only-token-with-at-least-32-chars")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"response": "reply"})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert post_chat("acct", "session", "hello") == "reply"
    assert calls[0][0] == f"{gateway_url.rstrip('/')}/api/chat"
    assert calls[0][1]["follow_redirects"] is False


def test_read_aloud_is_explicit_local_and_clears_after_new_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_ACCOUNTS_DB", str(tmp_path / "accounts.sqlite3"))
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "test-only-token-with-at-least-32-chars")
    synthesized = []
    tts_module = types.ModuleType("app.local_tts")

    def synthesize_wav_sync(text, language="en"):
        synthesized.append((text, language))
        return b"fake-wav"

    tts_module.synthesize_wav_sync = synthesize_wav_sync
    monkeypatch.setitem(sys.modules, "app.local_tts", tts_module)
    app = AppTest.from_file(APP_PATH).run()
    app.session_state["account_id"] = "acct-17"
    app.session_state["username"] = "demo-user"
    app.session_state["session_id"] = "session-a"
    app.session_state["messages"] = [{"role": "assistant", "content": "A reflective answer."}]
    app.run()

    assert synthesized == []
    assert any(button.label == "Read aloud" for button in app.button)
    language_select = next(
        item for item in app.selectbox if item.label == "Read the latest reply in"
    )
    language_select.set_value("Hindi").run()
    next(button for button in app.button if button.label == "Read aloud").click().run()
    assert synthesized == [("A reflective answer.", "hi")]
    assert app.session_state["reply_audio"] == b"fake-wav"

    app.session_state["messages"].append({"role": "user", "content": "new turn"})
    app.run()
    assert "reply_audio" not in app.session_state


def test_live_voice_receives_authenticated_ids_and_server_callbacks(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_ACCOUNTS_DB", str(tmp_path / "accounts.sqlite3"))
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "test-only-token-with-at-least-32-chars")
    calls = []
    live_module = types.ModuleType("app.live_voice")
    live_module.render_live_voice = lambda user_id, session_id, **kwargs: calls.append(
        (user_id, session_id, kwargs)
    )
    monkeypatch.setitem(sys.modules, "app.live_voice", live_module)

    app = AppTest.from_file(APP_PATH).run()
    app.session_state["account_id"] = "acct-17"
    app.session_state["username"] = "demo-user"
    app.session_state["session_id"] = "session-a"
    app.run()

    assert len(calls) == 1
    user_id, session_id, callbacks = calls[0]
    assert (user_id, session_id) == ("acct-17", "session-a")
    assert callbacks["transcribe"].__name__ == "post_transcription"
    assert callbacks["chat"].__name__ == "post_chat"


@pytest.mark.parametrize(
    "config", [{"ASTRO_GATEWAY_TOKEN": None}, {"GATEWAY_URL": "http://example.com"}]
)
def test_live_voice_does_not_mount_without_gateway_auth_and_safe_url(tmp_path, monkeypatch, config):
    monkeypatch.setenv("ASTRO_ACCOUNTS_DB", str(tmp_path / "accounts.sqlite3"))
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "test-only-token-with-at-least-32-chars")
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    calls = []
    live_module = types.ModuleType("app.live_voice")
    live_module.render_live_voice = lambda *args, **kwargs: calls.append((args, kwargs))
    monkeypatch.setitem(sys.modules, "app.live_voice", live_module)
    for key, value in config.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    app = AppTest.from_file(APP_PATH).run()
    app.session_state["account_id"] = "acct-17"
    app.session_state["username"] = "demo-user"
    app.session_state["session_id"] = "session-a"
    app.run()

    assert calls == []
    assert any("Live voice is unavailable" in item.value for item in app.info)


def test_reset_and_logout_release_live_voice_runtime_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRO_ACCOUNTS_DB", str(tmp_path / "accounts.sqlite3"))
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "test-only-token-with-at-least-32-chars")
    app = AppTest.from_file(APP_PATH).run()
    app.session_state["account_id"] = "acct-17"
    app.session_state["username"] = "demo-user"
    app.session_state["session_id"] = "session-a"
    app.run()

    class Runtime:
        def __init__(self):
            self.released = False

        def release(self):
            self.released = True

    first_runtime = Runtime()
    app.session_state["_live_voice_runtime"] = first_runtime
    app.button(key="new_conversation").click().run()
    assert first_runtime.released
    assert "_live_voice_runtime" not in app.session_state
    assert app.session_state["session_id"] != "session-a"

    second_runtime = Runtime()
    app.session_state["_live_voice_runtime"] = second_runtime
    app.button(key="sign_out").click().run()
    assert second_runtime.released
    assert "_live_voice_runtime" not in app.session_state
    assert not app.session_state.get("account_id")


def test_transcription_client_sends_raw_wav_with_server_bearer(monkeypatch):
    monkeypatch.setenv("ASTRO_GATEWAY_TOKEN", "server-only-token-with-at-least-32-chars")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"text": "recognized"})

    monkeypatch.setattr(httpx, "post", fake_post)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)

    assert post_transcription(output.getvalue()) == "recognized"
    assert calls[0][0] == "http://127.0.0.1:8000/api/transcribe"
    assert calls[0][1]["content"] == output.getvalue()
    assert (
        calls[0][1]["headers"]["Authorization"] == "Bearer server-only-token-with-at-least-32-chars"
    )
    assert calls[0][1]["headers"]["Content-Type"] == "audio/wav"
