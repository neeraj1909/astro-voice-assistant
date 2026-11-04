"""Local, session-owned voice turns for the Streamlit demo.

Completed turns go through buffered ASR and chat adapters; opted-in live mode
speaks replies locally. Sustained speech clears queued output and fences stale
results, but cannot roll back work already accepted by an upstream service.
"""

from __future__ import annotations

import io
import math
import queue
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable
import wave

import av
import streamlit as st
from streamlit_webrtc import (
    create_audio_sink_track,
    create_pcm_audio_source_track,
    webrtc_streamer,
)

INPUT_RATE = 16_000
OUTPUT_RATE = 24_000
FRAME_SECONDS = 0.02
PRE_ROLL_SECONDS = 0.16
MAX_UTTERANCE_SECONDS = 29.0
MAX_UTTERANCE_BYTES = int(INPUT_RATE * 2 * MAX_UTTERANCE_SECONDS)
MAX_PCM_OUTPUT_BYTES = int(OUTPUT_RATE * 2 * 30)
MAX_TEXT_LENGTH = 4_000
EVENT_QUEUE_SIZE = 32


@dataclass(frozen=True)
class TurnEvent:
    kind: str
    turn_id: int
    at: float
    response_turn_id: int | None = None


@dataclass(frozen=True)
class LiveVoiceEvent:
    kind: str
    turn_id: int
    text: str = ""
    message: str = ""


def _rms_pcm16(pcm: bytes) -> float:
    if not pcm or len(pcm) % 2:
        raise ValueError("PCM audio must contain complete signed 16-bit samples")
    count = len(pcm) // 2
    sum_squares = 0
    for (sample,) in struct.iter_unpack("<h", pcm):
        sum_squares += sample * sample
    return math.sqrt(sum_squares / count)


class EnergyTurnDetector:
    """A bounded, language-agnostic RMS VAD with onset/endpoint hysteresis."""

    def __init__(
        self,
        *,
        speech_start_seconds: float = 0.12,
        endpoint_silence_seconds: float = 0.60,
        energy_threshold: float = 600.0,
    ) -> None:
        if speech_start_seconds < 0 or endpoint_silence_seconds <= 0 or energy_threshold < 0:
            raise ValueError("VAD thresholds must be non-negative and endpoint silence positive")
        self.speech_start_seconds = speech_start_seconds
        self.endpoint_silence_seconds = endpoint_silence_seconds
        self.energy_threshold = energy_threshold
        self._next_turn_id = 1
        self._current_turn_id: int | None = None
        self._assistant_turn_id: int | None = None
        self._user_speaking = False
        self._speech_candidate_at: float | None = None
        self._silence_candidate_at: float | None = None
        self._last_at: float | None = None
        self._released = False

    @property
    def current_turn_id(self) -> int | None:
        return self._current_turn_id

    @property
    def is_user_speaking(self) -> bool:
        return self._user_speaking

    @property
    def is_assistant_speaking(self) -> bool:
        return self._assistant_turn_id is not None

    def is_response_current(self, turn_id: int) -> bool:
        return not self._released and turn_id == self._current_turn_id

    def assistant_started(self, turn_id: int) -> bool:
        if (
            self._released
            or self._user_speaking
            or self._assistant_turn_id is not None
            or turn_id != self._current_turn_id
        ):
            return False
        self._assistant_turn_id = turn_id
        return True

    def assistant_finished(self, turn_id: int) -> bool:
        if self._assistant_turn_id != turn_id:
            return False
        self._assistant_turn_id = None
        return True

    def observe_pcm(self, pcm: bytes, *, at: float) -> tuple[TurnEvent, ...]:
        if self._released:
            raise RuntimeError("turn detector has been released")
        if not math.isfinite(at):
            raise ValueError("timestamp must be finite and monotonic")
        if self._last_at is not None and at < self._last_at:
            raise ValueError("timestamp must be monotonic")
        self._last_at = at
        speech = _rms_pcm16(pcm) >= self.energy_threshold

        if self._user_speaking:
            if speech:
                self._silence_candidate_at = None
                return ()
            if self._silence_candidate_at is None:
                self._silence_candidate_at = at
                return ()
            if at - self._silence_candidate_at < self.endpoint_silence_seconds:
                return ()
            turn_id = self._current_turn_id
            self._user_speaking = False
            self._silence_candidate_at = None
            if turn_id is None:
                return ()
            return (TurnEvent("speech_end", turn_id, at),)

        if not speech:
            self._speech_candidate_at = None
            return ()
        if self._speech_candidate_at is None:
            self._speech_candidate_at = at
            return ()
        if at - self._speech_candidate_at < self.speech_start_seconds:
            return ()

        interrupted_turn_id = self._assistant_turn_id
        turn_id = self._next_turn_id
        self._next_turn_id += 1
        self._current_turn_id = turn_id
        self._user_speaking = True
        self._speech_candidate_at = None
        self._silence_candidate_at = None
        events = [TurnEvent("speech_start", turn_id, at)]
        if interrupted_turn_id is not None:
            self._assistant_turn_id = None
            events.append(TurnEvent("interrupt", turn_id, at, interrupted_turn_id))
        return tuple(events)

    def release(self) -> None:
        self._released = True
        self._current_turn_id = None
        self._assistant_turn_id = None
        self._user_speaking = False
        self._speech_candidate_at = None
        self._silence_candidate_at = None

    def reset(self) -> None:
        """Start a new lifecycle without ever reusing an earlier turn ID."""
        self._released = False
        self._current_turn_id = None
        self._assistant_turn_id = None
        self._user_speaking = False
        self._speech_candidate_at = None
        self._silence_candidate_at = None
        self._last_at = None


def _wav_to_pcm24k(wav_bytes: bytes) -> bytes:
    if not wav_bytes:
        raise ValueError("TTS returned empty audio")
    output = bytearray()
    with av.open(io.BytesIO(wav_bytes), format="wav") as source:
        if not source.streams.audio:
            raise ValueError("TTS output has no audio stream")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=OUTPUT_RATE)
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                output.extend(converted.to_ndarray().tobytes())
        for converted in resampler.resample(None):
            output.extend(converted.to_ndarray().tobytes())
    if not output or len(output) > MAX_PCM_OUTPUT_BYTES or len(output) % 2:
        raise ValueError("TTS output is empty or exceeds the audio limit")
    return bytes(output)


def _wav_from_pcm16(pcm: bytes, *, sample_rate: int = INPUT_RATE) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(pcm)
    return output.getvalue()


def _webrtc_audio_options(owner_key: str, source, sink) -> dict:
    """Bind the consuming sink and independent audio output track to WebRTC."""
    return {
        "key": f"live_voice_webrtc_{owner_key}",
        # The source factory returns a PcmAudioSource wrapper; aiortc needs its
        # MediaStreamTrack, while the runtime retains the wrapper for push/clear.
        "source_audio_track": source.track,
        "sink_audio_track": sink,
        # Explicit source_audio_track takes precedence over a sink-only path;
        # true is required by streamlit-webrtc's output-track send gate.
        "sendback_audio": True,
        "media_stream_constraints": {"audio": True, "video": False},
    }


class LiveVoiceRuntime:
    """Own one account/session's media, bounded turn jobs, and stale-result fence."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        transcribe: Callable[[bytes], str],
        chat: Callable[[str, str, str], str],
        synthesize: Callable[[str, str], bytes],
        source=None,
        detector: EnergyTurnDetector | None = None,
        language: str = "en",
    ) -> None:
        if not user_id or not session_id:
            raise ValueError("Live voice requires an authenticated user and session")
        self.user_id = user_id
        self.session_id = session_id
        self._transcribe = transcribe
        self._chat = chat
        self._synthesize = synthesize
        self._detector = detector or EnergyTurnDetector()
        self._source = source
        self._language = self._validate_language(language)
        self._lock = threading.RLock()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=INPUT_RATE)
        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_bytes = 0
        self._utterance = bytearray()
        self._utterance_turn_id: int | None = None
        self._utterance_too_long = False
        self._speaker_deadline: float | None = None
        self._playing_turn_id: int | None = None
        # Reservation spans synthesis, queued output, and playback. It prevents
        # automatic speech and the manual fallback from duplicating one turn.
        self._speech_turn_id: int | None = None
        self._latest_reply: tuple[int, str] | None = None
        self._released = threading.Event()
        self._jobs: queue.Queue[tuple[str, int, str | bytes, str] | None] = queue.Queue(maxsize=2)
        self._events: queue.Queue[LiveVoiceEvent] = queue.Queue(maxsize=EVENT_QUEUE_SIZE)
        self._workers = [
            threading.Thread(target=self._work, name=f"live-voice-session-{index}", daemon=True)
            for index in range(2)
        ]
        for worker in self._workers:
            worker.start()

    @property
    def owner(self) -> tuple[str, str]:
        return self.user_id, self.session_id

    @property
    def is_released(self) -> bool:
        return self._released.is_set()

    @property
    def latest_reply(self) -> tuple[int, str] | None:
        with self._lock:
            return self._latest_reply

    def can_read_aloud(self, turn_id: int) -> bool:
        with self._lock:
            return (
                self._speech_turn_id is None
                and self._latest_reply is not None
                and self._latest_reply[0] == turn_id
                and self._detector.is_response_current(turn_id)
            )

    def is_response_current(self, turn_id: int) -> bool:
        with self._lock:
            return self._detector.is_response_current(turn_id)

    def set_language(self, language: str) -> None:
        with self._lock:
            self._language = self._validate_language(language)

    @staticmethod
    def _validate_language(language: str) -> str:
        if language not in {"en", "hi", "bn"}:
            raise ValueError("read-aloud language must be en, hi, or bn")
        return language

    def attach_source(self, source) -> None:
        with self._lock:
            self._source = source

    def process_audio_frame(self, frame: av.AudioFrame) -> None:
        """Fast WebRTC sink callback; no Streamlit APIs or network calls here."""
        if self.is_released:
            return
        try:
            frames = self._resampler.resample(frame)
            for converted in frames:
                self.observe_pcm(converted.to_ndarray().tobytes(), at=time.monotonic())
        except (ValueError, RuntimeError):
            self._put_event(
                LiveVoiceEvent(
                    "error",
                    self._detector.current_turn_id or 0,
                    message="Microphone audio could not be processed.",
                )
            )

    def observe_pcm(self, pcm: bytes, *, at: float) -> tuple[TurnEvent, ...]:
        if self.is_released:
            raise RuntimeError("live voice runtime has been released")
        if len(pcm) > INPUT_RATE * 2:
            raise ValueError("audio frame exceeds the one-second frame limit")
        with self._lock:
            if self._speaker_deadline is not None and at >= self._speaker_deadline:
                if self._playing_turn_id is not None:
                    self._detector.assistant_finished(self._playing_turn_id)
                    self._playing_turn_id = None
                    self._speech_turn_id = None
                self._speaker_deadline = None
            self._pre_roll.append(pcm)
            self._pre_roll_bytes += len(pcm)
            while self._pre_roll and self._pre_roll_bytes > int(INPUT_RATE * 2 * PRE_ROLL_SECONDS):
                self._pre_roll_bytes -= len(self._pre_roll.popleft())

            events = self._detector.observe_pcm(pcm, at=at)
            start_event = next((event for event in events if event.kind == "speech_start"), None)
            if start_event is not None:
                if self._speech_turn_id is not None and self._speech_turn_id != start_event.turn_id:
                    # A barge-in also invalidates TTS that is still synthesizing
                    # and has not yet reached the output track.
                    self._speech_turn_id = None
                was_playing = self._detector.is_assistant_speaking
                # The detector has already cleared its speaker state while
                # emitting interrupt, so the source is cleared on that event.
                if any(event.kind == "interrupt" for event in events):
                    was_playing = True
                if was_playing and self._source is not None:
                    self._source.clear()
                    self._playing_turn_id = None
                    self._speech_turn_id = None
                self._speaker_deadline = None
                self._utterance = bytearray(b"".join(self._pre_roll))
                self._utterance_turn_id = start_event.turn_id
                self._utterance_too_long = len(self._utterance) > MAX_UTTERANCE_BYTES
            elif self._detector.is_user_speaking and self._utterance_turn_id is not None:
                if len(self._utterance) + len(pcm) <= MAX_UTTERANCE_BYTES:
                    self._utterance.extend(pcm)
                else:
                    self._utterance_too_long = True

            end_event = next((event for event in events if event.kind == "speech_end"), None)
            if end_event is not None:
                self._finish_utterance(end_event)
            for event in events:
                if event.kind == "interrupt":
                    self._put_event(
                        LiveVoiceEvent("interrupted", event.turn_id, message="Playback stopped.")
                    )
            return events

    def _finish_utterance(self, event: TurnEvent) -> None:
        turn_id = event.turn_id
        if self._utterance_too_long:
            self._put_event(
                LiveVoiceEvent(
                    "error",
                    turn_id,
                    message="That voice turn exceeded 29 seconds; try a shorter turn.",
                )
            )
        elif self._utterance:
            wav_bytes = _wav_from_pcm16(bytes(self._utterance))
            try:
                self._jobs.put_nowait(("turn", turn_id, wav_bytes, ""))
            except queue.Full:
                # Keep the newest user request and discard an older queued turn.
                try:
                    self._jobs.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._jobs.put_nowait(("turn", turn_id, wav_bytes, ""))
                except queue.Full:
                    self._put_event(
                        LiveVoiceEvent(
                            "error", turn_id, message="Voice processing is busy; try again shortly."
                        )
                    )
        self._utterance.clear()
        self._utterance_turn_id = None
        self._utterance_too_long = False

    def play_audio(self, turn_id: int, text: str, *, language: str) -> bool:
        """Queue explicitly requested speech for the latest non-stale response."""
        if self.is_released:
            return False
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            self._put_event(
                LiveVoiceEvent(
                    "error", turn_id, message="Read-aloud supports replies up to 500 characters."
                )
            )
            return False
        with self._lock:
            latest = self._latest_reply
            if (
                latest != (turn_id, text)
                or self._speech_turn_id is not None
                or not self._detector.is_response_current(turn_id)
            ):
                return False
            self._speech_turn_id = turn_id
            try:
                self._jobs.put_nowait(("speak", turn_id, text, language))
            except queue.Full:
                self._speech_turn_id = None
                self._put_event(
                    LiveVoiceEvent(
                        "error", turn_id, message="Voice processing is busy; try again shortly."
                    )
                )
                return False
            return True

    def _work(self) -> None:
        while not self.is_released:
            try:
                job = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            if job is None:
                return
            kind, turn_id, content, language = job
            if not self._current(turn_id):
                self._put_event(LiveVoiceEvent("stale_response", turn_id))
                continue
            try:
                if kind == "turn":
                    transcript = self._transcribe(content)  # type: ignore[arg-type]
                    if not self._current(turn_id):
                        self._put_event(LiveVoiceEvent("stale_response", turn_id))
                        continue
                    transcript = transcript.strip() if isinstance(transcript, str) else ""
                    if not transcript:
                        self._put_event(
                            LiveVoiceEvent("error", turn_id, message="No speech was recognized.")
                        )
                        continue
                    self._put_event(LiveVoiceEvent("user", turn_id, text=transcript))
                    reply = self._chat(self.user_id, self.session_id, transcript)
                    if not self._current(turn_id):
                        self._put_event(LiveVoiceEvent("stale_response", turn_id))
                        continue
                    reply = reply.strip() if isinstance(reply, str) else ""
                    if not reply:
                        self._put_event(
                            LiveVoiceEvent(
                                "error", turn_id, message="The chat service returned no reply."
                            )
                        )
                        continue
                    with self._lock:
                        if not self._detector.is_response_current(turn_id):
                            self._put_event(LiveVoiceEvent("stale_response", turn_id))
                            continue
                        self._latest_reply = (turn_id, reply)
                    self._put_event(LiveVoiceEvent("assistant", turn_id, text=reply))
                    self._speak(turn_id, reply, self._language, reserved=False)
                else:
                    self._speak(turn_id, content, language, reserved=True)  # type: ignore[arg-type]
            except Exception:
                # Don't put exception text, audio, or transcript content in UI/logs.
                message = (
                    "Local transcription failed."
                    if kind == "turn"
                    else "Local speech playback failed."
                )
                if self._current(turn_id):
                    self._put_event(LiveVoiceEvent("error", turn_id, message=message))

    def _speak(self, turn_id: int, text: str, language: str, *, reserved: bool) -> None:
        with self._lock:
            if not self._detector.is_response_current(turn_id):
                self._put_event(LiveVoiceEvent("stale_response", turn_id))
                return
            if reserved:
                if self._speech_turn_id != turn_id:
                    return
            else:
                if self._speech_turn_id is not None:
                    return
                self._speech_turn_id = turn_id
        try:
            wav_bytes = self._synthesize(text, language)
            pcm = _wav_to_pcm24k(wav_bytes)
        except Exception:
            with self._lock:
                if self._speech_turn_id == turn_id:
                    self._speech_turn_id = None
            if self._current(turn_id):
                self._put_event(
                    LiveVoiceEvent("error", turn_id, message="Local speech playback failed.")
                )
            return
        with self._lock:
            if not self._detector.is_response_current(turn_id):
                if self._speech_turn_id == turn_id:
                    self._speech_turn_id = None
                self._put_event(LiveVoiceEvent("stale_response", turn_id))
                return
            if self._source is None or not self._detector.assistant_started(turn_id):
                if self._speech_turn_id == turn_id:
                    self._speech_turn_id = None
                self._put_event(LiveVoiceEvent("stale_response", turn_id))
                return
            self._source.push(pcm)
            self._playing_turn_id = turn_id
            self._speaker_deadline = (
                max(self._last_audio_at or 0.0, time.monotonic())
                + (len(pcm) / 2 / OUTPUT_RATE)
                + 0.35
            )
            self._put_event(LiveVoiceEvent("speaking", turn_id))

    def _current(self, turn_id: int) -> bool:
        with self._lock:
            return not self.is_released and self._detector.is_response_current(turn_id)

    def _put_event(self, event: LiveVoiceEvent) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(event)
            except queue.Full:
                pass

    def drain_events(self) -> tuple[LiveVoiceEvent, ...]:
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return tuple(events)

    def release(self) -> None:
        if self._released.is_set():
            return
        with self._lock:
            if self._released.is_set():
                return
            self._released.set()
            self._detector.release()
            self._playing_turn_id = None
            self._speech_turn_id = None
            self._utterance.clear()
            self._pre_roll.clear()
            if self._source is not None:
                self._source.clear()
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break

    @property
    def _last_audio_at(self) -> float | None:
        return self._detector._last_at


_RUNTIME_KEY = "_live_voice_runtime"


def _owner_key(user_id: str, session_id: str) -> str:
    import hashlib

    return hashlib.sha256(f"{user_id}\0{session_id}".encode()).hexdigest()[:16]


@st.fragment(run_every=0.2)
def _render_runtime(runtime: LiveVoiceRuntime, owner_key: str, language: str) -> None:
    for event in runtime.drain_events():
        if event.kind in {"user", "assistant"}:
            _clear_reply_audio_state()
            messages = st.session_state.setdefault("messages", [])
            messages.append({"role": event.kind, "content": event.text})
        elif event.kind == "error":
            st.warning(event.message)
        elif event.kind == "interrupted":
            st.caption("Reply stopped. Listening for your next turn.")

    live_messages = [
        message
        for message in st.session_state.get("messages", [])
        if message.get("role") in {"user", "assistant"}
    ][-12:]
    with st.container(height=260, border=True):
        for message in live_messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

    latest = runtime.latest_reply
    if latest is not None:
        turn_id, text = latest
        if st.button(
            "Read latest reply aloud",
            key=f"live_voice_read_{owner_key}",
            disabled=(
                len(text) > 500 or runtime.is_released or not runtime.can_read_aloud(turn_id)
            ),
        ):
            if runtime.play_audio(turn_id, text, language=language):
                st.caption("Preparing local speech. The microphone stays active during playback.")
        if len(text) > 500:
            st.caption("Local read-aloud is limited to 500 characters per reply.")


def render_live_voice(
    user_id: str,
    session_id: str,
    *,
    transcribe: Callable[[bytes], str],
    chat: Callable[[str, str, str], str],
) -> None:
    """Render consent-gated live microphone capture for one authenticated session.

    Gateway adapter callables remain server-side. This function never passes a
    bearer credential or account ID to the browser; the WebRTC component receives
    only media tracks and opaque Streamlit component state.
    """
    owner_key = _owner_key(user_id, session_id)
    owner = (user_id, session_id)
    runtime = st.session_state.get(_RUNTIME_KEY)
    if runtime is not None and (runtime.owner != owner or runtime.is_released):
        runtime.release()
        st.session_state.pop(_RUNTIME_KEY, None)
        runtime = None

    st.markdown("#### Live voice (prototype)")
    st.caption(
        "Live audio is streamed to this app and endpointed locally with a simple energy VAD. "
        "Completed turns use the configured local Whisper service, then send recognized text "
        "to the configured chat service. Spoken replies are synthesized locally. If a buffered "
        "ASR/chat request is already in flight when you interrupt, this UI suppresses its late "
        "reply but cannot cancel the request or undo an upstream conversation update."
    )
    language = st.selectbox(
        "Read-aloud voice",
        options=("en", "hi", "bn"),
        format_func={"en": "English", "hi": "हिन्दी", "bn": "বাংলা"}.__getitem__,
        key=f"live_voice_language_{owner_key}",
    )
    consent = st.checkbox(
        "I consent to stream microphone audio and automatically play local spoken replies while live voice is active.",
        key=f"live_voice_consent_{owner_key}",
    )
    if not consent:
        if runtime is not None:
            runtime.release()
            st.session_state.pop(_RUNTIME_KEY, None)
        return

    if runtime is None:
        from app.local_tts import synthesize_wav_sync

        runtime = LiveVoiceRuntime(
            user_id=user_id,
            session_id=session_id,
            transcribe=transcribe,
            chat=chat,
            synthesize=synthesize_wav_sync,
        )
        st.session_state[_RUNTIME_KEY] = runtime
    runtime.set_language(language)

    source = create_pcm_audio_source_track(
        key=f"live_voice_source_{owner_key}",
        sample_rate=OUTPUT_RATE,
        ptime=FRAME_SECONDS,
    )
    runtime.attach_source(source)
    sink = create_audio_sink_track(
        callback=runtime.process_audio_frame,
        key=f"live_voice_sink_{owner_key}",
        on_ended=runtime.release,
    )
    webrtc_streamer(**_webrtc_audio_options(owner_key, source, sink))
    st.caption(
        "The microphone remains active during read-aloud. Sustained speech clears queued playback. "
        "This RMS VAD is sensitive to microphone gain and background noise."
    )
    _render_runtime(runtime, owner_key, language)


def release_live_voice() -> None:
    """Release the current browser session's runtime on logout or conversation reset."""
    runtime = st.session_state.get(_RUNTIME_KEY)
    if runtime is not None:
        runtime.release()
        st.session_state.pop(_RUNTIME_KEY, None)
    _clear_reply_audio_state()


def _clear_reply_audio_state() -> None:
    exact = {"reply_audio", "reply_audio_identity", "reply_audio_message_count"}
    for key in tuple(st.session_state.keys()):
        if key in exact or key.startswith("read_aloud_text_"):
            st.session_state.pop(key, None)
