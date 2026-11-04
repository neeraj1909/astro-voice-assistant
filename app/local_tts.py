"""Bounded, offline eSpeak-NG speech synthesis without audio files."""

import asyncio
import ctypes
import io
import subprocess
import sys
import threading
import wave
from pathlib import Path

MAX_TEXT_CHARACTERS = 500
MAX_AUDIO_SECONDS = 30
SYNTHESIS_TIMEOUT_SECONDS = 15
SAMPLE_RATE = 22050
_SUPPORTED_VOICES = {"en": "en", "hi": "hi", "bn": "bn"}
_PROCESS_LOCK = threading.Lock()
_WORKER_TEXT_BYTES = 4096


class SynthesisError(RuntimeError):
    """Base class for expected local TTS failures."""


class InvalidSpeechText(SynthesisError):
    """Raised for blank or oversized input text."""


class UnsupportedLanguage(SynthesisError):
    """Raised when no installed voice is mapped for the requested language."""


class SynthesisUnavailable(SynthesisError):
    """Raised when the local eSpeak-NG engine fails or is not installed."""


class SynthesisTimeout(SynthesisError):
    """Raised when isolated speech synthesis exceeds its deadline."""


class SynthesisBusy(SynthesisError):
    """Raised when another local synthesis is already running."""


class SynthesisTooLong(SynthesisError):
    """Raised when generated audio exceeds the response duration limit."""


def _worker(voice: str) -> int:
    """Run in the isolated child process; write only WAV bytes to stdout."""
    text = sys.stdin.buffer.read(_WORKER_TEXT_BYTES + 1)
    if not text or len(text) > _WORKER_TEXT_BYTES:
        return 10

    engine = None
    try:
        engine = ctypes.CDLL("libespeak-ng.so.1")
        callback_type = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p
        )
        chunks: list[bytes] = []

        @callback_type
        def collect_audio(samples, count, _events):
            if samples and count > 0:
                chunks.append(ctypes.string_at(samples, count * 2))
            return 0

        engine.espeak_Initialize.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        engine.espeak_Initialize.restype = ctypes.c_int
        engine.espeak_SetSynthCallback.argtypes = [callback_type]
        engine.espeak_SetSynthCallback.restype = ctypes.c_int
        engine.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
        engine.espeak_SetVoiceByName.restype = ctypes.c_int
        engine.espeak_Synth.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.c_void_p,
        ]
        engine.espeak_Synth.restype = ctypes.c_int
        engine.espeak_Synchronize.restype = ctypes.c_int
        engine.espeak_Terminate.restype = ctypes.c_int

        sample_rate = engine.espeak_Initialize(1, 100, None, 0)
        if sample_rate != SAMPLE_RATE:
            return 11
        if engine.espeak_SetSynthCallback(collect_audio) != 0:
            return 11
        if engine.espeak_SetVoiceByName(voice.encode("ascii")) != 0:
            return 11

        identifier = ctypes.c_uint(0)
        if (
            engine.espeak_Synth(
                ctypes.c_char_p(text),
                len(text),
                0,
                1,
                0,
                1,
                ctypes.byref(identifier),
                None,
            )
            != 0
        ):
            return 11
        if engine.espeak_Synchronize() != 0:
            return 11

        pcm = b"".join(chunks)
        if not pcm:
            return 11
        if len(pcm) > sample_rate * 2 * MAX_AUDIO_SECONDS:
            return 12

        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        sys.stdout.buffer.write(output.getvalue())
        sys.stdout.buffer.flush()
        return 0
    finally:
        if engine is not None:
            engine.espeak_Terminate()


def _prepare(text: str, language: str) -> tuple[bytes, str]:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
        raise InvalidSpeechText("Text must contain 1–500 characters")
    if "\0" in text:
        raise InvalidSpeechText("Text contains an invalid character")
    try:
        encoded = text.strip().encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidSpeechText("Text must be valid Unicode") from exc

    if not isinstance(language, str):
        raise UnsupportedLanguage("Choose an installed language voice")
    code = language.strip().lower().replace("_", "-").split("-", 1)[0]
    try:
        voice = _SUPPORTED_VOICES[code]
    except KeyError as exc:
        raise UnsupportedLanguage("Choose an installed language voice") from exc
    return encoded, voice


def _validate_wave(audio: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            rate = wav.getframerate()
            frames = wav.getnframes()
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or rate != SAMPLE_RATE:
                raise SynthesisUnavailable("Local speech engine returned an invalid audio format")
            if frames <= 0 or len(wav.readframes(frames)) != frames * 2:
                raise SynthesisUnavailable("Local speech engine returned invalid audio")
            if frames / rate > MAX_AUDIO_SECONDS:
                raise SynthesisTooLong("Generated audio exceeds the duration limit")
    except (EOFError, OSError, wave.Error) as exc:
        raise SynthesisUnavailable("Local speech engine returned invalid audio") from exc
    return audio


def _command(voice: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--worker", voice]


def synthesize_wav_sync(text: str, language: str = "en") -> bytes:
    """Blocking adapter for worker threads; returns mono PCM16 WAV bytes."""
    encoded, voice = _prepare(text, language)
    if not _PROCESS_LOCK.acquire(blocking=False):
        raise SynthesisBusy("Speech synthesis is busy; try again shortly")
    try:
        try:
            result = subprocess.run(
                _command(voice),
                input=encoded,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=SYNTHESIS_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SynthesisTimeout("Speech synthesis timed out") from exc
        except OSError as exc:
            raise SynthesisUnavailable("Local speech synthesis is unavailable") from exc
        if result.returncode == 12:
            raise SynthesisTooLong("Generated audio exceeds the duration limit")
        if result.returncode != 0:
            raise SynthesisUnavailable("Local speech synthesis failed")
        return _validate_wave(result.stdout)
    finally:
        _PROCESS_LOCK.release()


async def synthesize_wav(text: str, language: str = "en") -> bytes:
    """Async adapter for Streamlit or other event-loop callers."""
    encoded, voice = _prepare(text, language)
    if not _PROCESS_LOCK.acquire(blocking=False):
        raise SynthesisBusy("Speech synthesis is busy; try again shortly")

    process: asyncio.subprocess.Process | None = None
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *_command(voice),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SynthesisUnavailable("Local speech synthesis is unavailable") from exc

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(input=encoded), timeout=SYNTHESIS_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise SynthesisTimeout("Speech synthesis timed out") from exc
        except asyncio.CancelledError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise

        if process.returncode == 12:
            raise SynthesisTooLong("Generated audio exceeds the duration limit")
        if process.returncode != 0:
            raise SynthesisUnavailable("Local speech synthesis failed")
        return _validate_wave(stdout)
    finally:
        _PROCESS_LOCK.release()


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    sys.exit(_worker(sys.argv[2]))
