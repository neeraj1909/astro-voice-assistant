import asyncio
import io
import os
from pathlib import Path
import wave

from fastapi.testclient import TestClient

from app.main import create_app

GATEWAY_TOKEN = "test-gateway-token-" + "x" * 32


def wav_bytes(
    *, seconds: float = 1, sample_width: int = 2, channels: int = 1, sample_rate: int = 16000
) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.setframerate(sample_rate)
        output.writeframes(b"\0" * int(sample_rate * seconds) * channels * sample_width)
    return buffer.getvalue()


def post_wav(
    client,
    content: bytes,
    content_type: str = "audio/wav",
    token: str = GATEWAY_TOKEN,
):
    headers = {"content-type": content_type}
    if token:
        headers["authorization"] = f"Bearer {token}"
    return client.post("/api/transcribe", content=content, headers=headers)


class CompletedProcess:
    returncode = 0

    async def communicate(self):
        return b"", b""

    async def wait(self):
        return 0

    def kill(self):
        pass


def test_transcribe_uses_configured_local_cli_and_returns_text(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    temp_dirs: list[Path] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        assert "shell" not in kwargs
        wav_path = Path(args[-1])
        temp_dirs.append(wav_path.parent)
        output_prefix = Path(args[args.index("-of") + 1])
        output_prefix.with_suffix(".txt").write_text("hello from local whisper\n")
        assert wav_path.is_file()
        assert os.stat(wav_path.parent).st_mode & 0o077 == 0
        assert os.stat(wav_path).st_mode & 0o077 == 0
        with wave.open(str(wav_path), "rb") as audio:
            assert audio.getframerate() == 16000
        return CompletedProcess()

    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/ggml-tiny.en.bin")
    monkeypatch.setattr("app.local_asr.asyncio.create_subprocess_exec", fake_exec)
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes())

    assert response.status_code == 200
    assert response.json() == {"text": "hello from local whisper"}
    assert calls[0][:5] == (
        "/local/whisper-cli",
        "-m",
        "/local/ggml-tiny.en.bin",
        "-t",
        "2",
    )
    assert calls[0][calls[0].index("-l") + 1] == "en"
    assert calls[0][calls[0].index("-f") + 1].endswith(".wav")
    assert len(temp_dirs) == 1 and not temp_dirs[0].exists()


def test_transcribe_supports_auto_language_for_multilingual_model(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        Path(args[args.index("-of") + 1]).with_suffix(".txt").write_text("नमस्ते")
        return CompletedProcess()

    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/ggml-small.bin")
    monkeypatch.setenv("WHISPER_LANGUAGE", "auto")
    monkeypatch.setattr("app.local_asr.asyncio.create_subprocess_exec", fake_exec)
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes())

    assert response.status_code == 200
    assert calls[0][calls[0].index("-l") + 1] == "auto"


def test_transcribe_supports_explicit_indic_language(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        Path(args[args.index("-of") + 1]).with_suffix(".txt").write_text("বাংলা")
        return CompletedProcess()

    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/ggml-small.bin")
    monkeypatch.setenv("WHISPER_LANGUAGE", "BN")
    monkeypatch.setattr("app.local_asr.asyncio.create_subprocess_exec", fake_exec)
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes())

    assert response.status_code == 200
    assert calls[0][calls[0].index("-l") + 1] == "bn"


def test_transcribe_rejects_unsafe_language_without_invoking_cli(monkeypatch) -> None:
    invoked = False

    async def fake_exec(*args, **kwargs):
        nonlocal invoked
        invoked = True
        return CompletedProcess()

    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/ggml-small.bin")
    monkeypatch.setenv("WHISPER_LANGUAGE", "--help")
    monkeypatch.setattr("app.local_asr.asyncio.create_subprocess_exec", fake_exec)
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes())

    assert response.status_code == 503
    assert "WHISPER_LANGUAGE" in response.json()["detail"]
    assert not invoked


def test_auto_language_rejects_named_english_only_model(monkeypatch) -> None:
    invoked = False

    async def fake_exec(*args, **kwargs):
        nonlocal invoked
        invoked = True
        return CompletedProcess()

    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/ggml-tiny.en.bin")
    monkeypatch.setenv("WHISPER_LANGUAGE", "auto")
    monkeypatch.setattr("app.local_asr.asyncio.create_subprocess_exec", fake_exec)
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes())

    assert response.status_code == 503
    assert "English-only" in response.json()["detail"]
    assert not invoked


def test_transcribe_requires_raw_wav_content_type(monkeypatch) -> None:
    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/model.bin")
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes(), "audio/webm")
    assert response.status_code == 415


def test_transcribe_rejects_non_pcm_wav(monkeypatch) -> None:
    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/model.bin")
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes(sample_width=1))
    assert response.status_code == 422


def test_transcribe_requires_mono_16khz_audio(monkeypatch) -> None:
    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/model.bin")
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        stereo = post_wav(client, wav_bytes(channels=2))
        wrong_rate = post_wav(client, wav_bytes(sample_rate=8000))
    assert stereo.status_code == 422
    assert wrong_rate.status_code == 422


def test_transcribe_rejects_audio_over_duration_limit(monkeypatch) -> None:
    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/model.bin")
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes(seconds=31))
    assert response.status_code == 413


def test_transcribe_requires_local_cli_configuration(monkeypatch) -> None:
    monkeypatch.delenv("WHISPER_CLI", raising=False)
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes())
    assert response.status_code == 503


def test_transcribe_enforces_streamed_body_size_limit(monkeypatch) -> None:
    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/model.bin")
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, b"x" * (8 * 1024 * 1024 + 1))
    assert response.status_code == 413


def test_transcribe_timeout_kills_process_and_cleans_temp_file(monkeypatch) -> None:
    captured: list[Path] = []
    killed: list[bool] = []

    class SlowProcess(CompletedProcess):
        async def communicate(self):
            await asyncio.sleep(1)
            return b"", b""

        def kill(self):
            killed.append(True)

    async def fake_exec(*args, **kwargs):
        captured.append(Path(args[-1]).parent)
        return SlowProcess()

    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/model.bin")
    monkeypatch.setattr("app.local_asr.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.local_asr.TRANSCRIPTION_TIMEOUT_SECONDS", 0.01)
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        response = post_wav(client, wav_bytes())
    assert response.status_code == 504
    assert killed == [True]
    assert len(captured) == 1 and not captured[0].exists()


def test_transcribe_rejects_missing_or_invalid_gateway_token(monkeypatch) -> None:
    called = False

    async def fake_exec(*args, **kwargs):
        nonlocal called
        called = True
        Path(args[args.index("-of") + 1] + ".txt").write_text("should not run")
        return CompletedProcess()

    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/model.bin")
    monkeypatch.setattr("app.local_asr.asyncio.create_subprocess_exec", fake_exec)
    with TestClient(create_app("http://chat.test/chat", gateway_token=GATEWAY_TOKEN)) as client:
        missing = post_wav(client, wav_bytes(), token="")
        invalid = post_wav(client, wav_bytes(), token="wrong-token")

    assert missing.status_code == invalid.status_code == 401
    assert not called


def test_transcribe_fails_closed_when_gateway_token_is_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv("WHISPER_CLI", "/local/whisper-cli")
    monkeypatch.setenv("WHISPER_MODEL", "/local/model.bin")
    with TestClient(create_app("http://chat.test/chat", gateway_token="short")) as client:
        response = post_wav(client, wav_bytes())
    assert response.status_code == 503
