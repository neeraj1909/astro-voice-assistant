import asyncio
import ctypes.util
import io
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from app.local_tts import (
    _PROCESS_LOCK,
    InvalidSpeechText,
    SynthesisBusy,
    SynthesisTimeout,
    SynthesisTooLong,
    SynthesisUnavailable,
    UnsupportedLanguage,
    synthesize_wav,
    synthesize_wav_sync,
)


def wav_bytes(seconds: float = 1, rate: int = 22050, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x01\0" * int(seconds * rate) * channels)
    return buffer.getvalue()


class FakeProcess:
    returncode = 0

    def __init__(self, audio: bytes):
        self.audio = audio
        self.input = None
        self.killed = False
        self.reaped = False

    async def communicate(self, input=None):
        self.input = input
        return self.audio, b"private diagnostic"

    async def wait(self):
        self.reaped = True

    def kill(self):
        self.killed = True


def run(coro):
    return asyncio.run(coro)


def test_async_synthesis_returns_pcm_wav_and_uses_voice_without_shell(monkeypatch) -> None:
    process = FakeProcess(wav_bytes())
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)
    result = run(synthesize_wav("Hello", language="en"))

    with wave.open(io.BytesIO(result), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 22050
        assert audio.getnframes() == 22050
    args, kwargs = calls[0]
    assert args[0:2] == (sys.executable, str(Path("app/local_tts.py").resolve()))
    assert args[2:4] == ("--worker", "en")
    assert process.input == b"Hello"
    assert "shell" not in kwargs
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert not _PROCESS_LOCK.locked()


def test_language_codes_are_restricted_to_local_voices(monkeypatch) -> None:
    seen = []

    async def fake_exec(*args, **kwargs):
        seen.append(args[3])
        return FakeProcess(wav_bytes())

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)
    for language in ("en", "hi-IN", "bn-BD"):
        run(synthesize_wav("hello", language=language))
    assert seen == ["en", "hi", "bn"]

    with pytest.raises(UnsupportedLanguage):
        run(synthesize_wav("hello", language="fr"))


def test_input_is_bounded_before_starting_process(monkeypatch) -> None:
    called = False

    async def fake_exec(*args, **kwargs):
        nonlocal called
        called = True
        return FakeProcess(wav_bytes())

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(InvalidSpeechText):
        run(synthesize_wav("x" * 501))
    assert not called


def test_output_duration_is_bounded(monkeypatch) -> None:
    async def fake_exec(*args, **kwargs):
        return FakeProcess(wav_bytes(seconds=31))

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(SynthesisTooLong):
        run(synthesize_wav("hello"))


@pytest.mark.parametrize("audio", [b"not a wave", wav_bytes(channels=2), wav_bytes(rate=16000)])
def test_invalid_or_unexpected_engine_audio_fails_safely(monkeypatch, audio) -> None:
    async def fake_exec(*args, **kwargs):
        return FakeProcess(audio)

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(SynthesisUnavailable):
        run(synthesize_wav("hello"))


def test_busy_synthesis_is_rejected_without_waiting(monkeypatch) -> None:
    called = False

    async def fake_exec(*args, **kwargs):
        nonlocal called
        called = True
        return FakeProcess(wav_bytes())

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)
    _PROCESS_LOCK.acquire()
    try:
        with pytest.raises(SynthesisBusy):
            run(synthesize_wav("hello"))
    finally:
        _PROCESS_LOCK.release()
    assert not called


def test_timeout_kills_and_reaps_child(monkeypatch) -> None:
    process = FakeProcess(wav_bytes())

    async def fake_exec(*args, **kwargs):
        async def blocked(input=None):
            await asyncio.sleep(1)
            return process.audio, b""

        process.communicate = blocked
        return process

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.local_tts.SYNTHESIS_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(SynthesisTimeout):
        run(synthesize_wav("hello"))
    assert process.killed and process.reaped
    assert not _PROCESS_LOCK.locked()


def test_cancellation_kills_and_reaps_child(monkeypatch) -> None:
    process = FakeProcess(wav_bytes())
    started = None

    async def fake_exec(*args, **kwargs):
        nonlocal started
        started = asyncio.Event()

        async def blocked(input=None):
            started.set()
            await asyncio.Future()

        process.communicate = blocked
        return process

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)

    async def cancel_synthesis():
        task = asyncio.create_task(synthesize_wav("hello"))
        while started is None or not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(cancel_synthesis())
    assert process.killed and process.reaped
    assert not _PROCESS_LOCK.locked()


def test_child_failure_does_not_return_stderr(monkeypatch) -> None:
    class FailedProcess(FakeProcess):
        returncode = 2

        async def communicate(self, input=None):
            return b"", b"private diagnostic"

    async def fake_exec(*args, **kwargs):
        return FailedProcess(b"")

    monkeypatch.setattr("app.local_tts.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(SynthesisUnavailable) as failure:
        run(synthesize_wav("hello"))
    assert "private diagnostic" not in str(failure.value)


def test_sync_adapter_uses_same_local_worker(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=wav_bytes(), stderr=b"private")

    monkeypatch.setattr("app.local_tts.subprocess.run", fake_run)
    result = synthesize_wav_sync("नमस्ते", language="hi")
    assert result.startswith(b"RIFF")
    assert calls[0][0][3] == "hi"
    assert "shell" not in calls[0][1]


def test_sync_adapter_maps_timeout_to_safe_error(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr("app.local_tts.subprocess.run", fake_run)
    with pytest.raises(SynthesisTimeout):
        synthesize_wav_sync("hello")


@pytest.mark.skipif(
    ctypes.util.find_library("espeak-ng") is None,
    reason="local eSpeak-NG shared library is not installed",
)
def test_installed_espeak_generates_english_hindi_and_bengali_wav() -> None:
    phrases = {
        "en": "Hello, the local reply is ready.",
        "hi": "नमस्ते, स्थानीय उत्तर तैयार है।",
        "bn": "নমস্কার, স্থানীয় উত্তর প্রস্তুত।",
    }
    for language, phrase in phrases.items():
        result = synthesize_wav_sync(phrase, language)
        with wave.open(io.BytesIO(result), "rb") as audio:
            samples = audio.readframes(audio.getnframes())
            assert audio.getnchannels() == 1
            assert audio.getsampwidth() == 2
            assert audio.getframerate() == 22050
            assert 0 < audio.getnframes() <= 22050 * 30
            assert any(samples)
