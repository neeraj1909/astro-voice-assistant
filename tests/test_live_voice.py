from __future__ import annotations

import io
import threading
import time
import wave

import pytest

from app.live_voice import (
    EnergyTurnDetector,
    LiveVoiceRuntime,
    _record_live_voice_event,
    _webrtc_audio_options,
)


def pcm(amplitude: int, *, samples: int = 320) -> bytes:
    return b"".join(int(amplitude).to_bytes(2, "little", signed=True) for _ in range(samples))


def wav(pcm_bytes: bytes, *, sample_rate: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(pcm_bytes)
    return output.getvalue()


def feed(detector: EnergyTurnDetector, values: list[tuple[float, int]]):
    events = []
    for at, amplitude in values:
        events.extend(detector.observe_pcm(pcm(amplitude), at=at))
    return events


def wait_for(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakePcmSource:
    def __init__(self):
        self.pushed: list[bytes] = []
        self.clear_count = 0

    def push(self, audio: bytes) -> None:
        self.pushed.append(bytes(audio))

    def clear(self) -> None:
        self.clear_count += 1
        self.pushed.clear()


def test_energy_detector_filters_short_noise_and_endpoint_pauses():
    detector = EnergyTurnDetector(
        speech_start_seconds=0.10,
        endpoint_silence_seconds=0.50,
        energy_threshold=600,
    )

    events = feed(
        detector,
        [
            (0.00, 3000),
            (0.04, 0),  # transient sound is shorter than the onset hysteresis
            (0.10, 3000),
            (0.16, 3000),
            (0.22, 3000),  # speech starts after sustained energy
            (0.30, 0),
            (0.70, 0),  # conversational pause is not an endpoint
            (0.74, 3000),
            (0.84, 3000),
            (0.90, 0),
            (1.38, 0),
            (1.41, 0),  # the full silence window ends the turn
        ],
    )

    assert [(event.kind, event.turn_id) for event in events] == [
        ("speech_start", 1),
        ("speech_end", 1),
    ]
    assert events[0].at == pytest.approx(0.22)
    assert events[1].at == pytest.approx(1.41)


def test_sustained_barge_in_clears_playback_and_invalidates_old_turn():
    detector = EnergyTurnDetector(speech_start_seconds=0.10, endpoint_silence_seconds=0.30)
    source = FakePcmSource()
    runtime = LiveVoiceRuntime(
        user_id="acct-1",
        session_id="session-1",
        transcribe=lambda _: "hello",
        chat=lambda *_: "answer",
        synthesize=lambda *_: wav(pcm(1000)),
        source=source,
        detector=detector,
    )
    try:
        runtime.observe_pcm(pcm(3000), at=0.0)
        started = runtime.observe_pcm(pcm(3000), at=0.1)
        first_turn = started[0].turn_id
        runtime.observe_pcm(pcm(0), at=0.2)
        runtime.observe_pcm(pcm(0), at=0.5)
        assert wait_for(lambda: runtime.latest_reply is not None and bool(source.pushed))
        assert runtime.latest_reply == (first_turn, "answer")
        assert detector.is_assistant_speaking

        assert runtime.observe_pcm(pcm(0), at=0.6) == ()
        assert runtime.observe_pcm(pcm(4000), at=0.7) == ()
        events = runtime.observe_pcm(pcm(4000), at=0.8)

        assert [event.kind for event in events] == ["speech_start", "interrupt"]
        assert events[-1].response_turn_id == first_turn
        assert source.clear_count == 1
        assert not runtime.is_response_current(first_turn)
        assert not detector.is_assistant_speaking

        live_events = runtime.drain_events()
        assistant = next(event for event in live_events if event.kind == "assistant")
        interrupted = next(event for event in live_events if event.kind == "interrupted")
        assert assistant.turn_id == first_turn
        assert interrupted.response_turn_id == first_turn
        assert not any(
            event.kind == "user" and event.turn_id != first_turn for event in live_events
        )

        messages = []
        _record_live_voice_event(messages, assistant)
        _record_live_voice_event(messages, interrupted)
        _record_live_voice_event(messages, interrupted)
        assert messages == [
            {
                "role": "assistant",
                "content": "answer",
                "voice_turn_id": first_turn,
                "voice_playback_interrupted": True,
            }
        ]
    finally:
        runtime.release()


def test_endpoint_sends_bounded_wav_through_session_owned_gateway_calls():
    source = FakePcmSource()
    transcriptions: list[bytes] = []
    chat_calls: list[tuple[str, str, str]] = []
    tts_calls: list[tuple[str, str]] = []

    def transcribe(audio: bytes) -> str:
        transcriptions.append(audio)
        return "What is on my mind?"

    def chat(user_id: str, session_id: str, text: str) -> str:
        chat_calls.append((user_id, session_id, text))
        return "Take a quiet moment to reflect."

    def synthesize(text: str, language: str) -> bytes:
        tts_calls.append((text, language))
        return wav(pcm(1000, samples=1600))

    runtime = LiveVoiceRuntime(
        user_id="acct-7",
        session_id="session-9",
        transcribe=transcribe,
        chat=chat,
        synthesize=synthesize,
        source=source,
        detector=EnergyTurnDetector(
            speech_start_seconds=0.04,
            endpoint_silence_seconds=0.10,
            energy_threshold=600,
        ),
    )
    try:
        for at, amplitude in [(0.0, 2500), (0.04, 2500), (0.08, 2500), (0.12, 0), (0.23, 0)]:
            runtime.observe_pcm(pcm(amplitude), at=at)

        assert wait_for(lambda: bool(chat_calls))
        events = runtime.drain_events()
        assert [event.kind for event in events if event.kind in {"user", "assistant"}] == [
            "user",
            "assistant",
        ]
        assert chat_calls == [("acct-7", "session-9", "What is on my mind?")]
        assert len(transcriptions) == 1
        with wave.open(io.BytesIO(transcriptions[0]), "rb") as captured:
            assert (captured.getnchannels(), captured.getsampwidth(), captured.getframerate()) == (
                1,
                2,
                16000,
            )

        # Spoken output follows a completed voice turn automatically.
        assert wait_for(lambda: bool(source.pushed))
        assert tts_calls == [("Take a quiet moment to reflect.", "auto")]
        assistant = next(event for event in events if event.kind == "assistant")
        assert assistant.turn_id == runtime.latest_reply[0]
    finally:
        runtime.release()


def test_manual_replay_is_rejected_while_automatic_tts_is_pending_or_playing():
    synth_entered = threading.Event()
    finish_synth = threading.Event()
    synth_calls: list[tuple[str, str]] = []
    source = FakePcmSource()

    def synthesize(text: str, language: str) -> bytes:
        synth_calls.append((text, language))
        synth_entered.set()
        assert finish_synth.wait(2.0)
        return wav(pcm(1000, samples=1600))

    runtime = LiveVoiceRuntime(
        user_id="acct-1",
        session_id="session-1",
        transcribe=lambda _: "hello",
        chat=lambda *_: "one spoken reply",
        synthesize=synthesize,
        source=source,
        detector=EnergyTurnDetector(
            speech_start_seconds=0.04,
            endpoint_silence_seconds=0.10,
            energy_threshold=600,
        ),
    )
    try:
        for at, amplitude in [(0.0, 2500), (0.04, 2500), (0.08, 2500), (0.12, 0), (0.23, 0)]:
            runtime.observe_pcm(pcm(amplitude), at=at)

        assert synth_entered.wait(1.0)
        turn_id, reply = runtime.latest_reply
        assert not runtime.can_read_aloud(turn_id)
        assert not runtime.play_audio(turn_id, reply, language="en")

        finish_synth.set()
        assert wait_for(lambda: bool(source.pushed))
        assert synth_calls == [("one spoken reply", "auto")]
        assert len(source.pushed) == 1
        assert not runtime.can_read_aloud(turn_id)
        assert not runtime.play_audio(turn_id, reply, language="en")
    finally:
        finish_synth.set()
        runtime.release()


def test_status_tracks_processing_speech_and_interruption():
    entered = threading.Event()
    finish = threading.Event()
    source = FakePcmSource()

    def transcribe(_audio):
        entered.set()
        assert finish.wait(3)
        return "hello"

    runtime = LiveVoiceRuntime(
        user_id="user",
        session_id="session",
        transcribe=transcribe,
        chat=lambda *_: "hello back",
        synthesize=lambda *_: wav(pcm(1000, samples=1600)),
        source=source,
        detector=EnergyTurnDetector(speech_start_seconds=0.04, endpoint_silence_seconds=0.1),
    )
    try:
        for at, amplitude in [(0, 2500), (0.04, 2500), (0.08, 2500), (0.12, 0), (0.23, 0)]:
            runtime.observe_pcm(pcm(amplitude), at=at)
        assert entered.wait(1)
        assert runtime.status == "Thinking…"
        finish.set()
        assert wait_for(lambda: bool(source.pushed))
        assert runtime.status == "Speaking"
        for at in (0.30, 0.35, 0.40):
            runtime.observe_pcm(pcm(2500), at=at)
        assert runtime.status == "Listening to you"
        assert source.clear_count > 0
    finally:
        finish.set()
        runtime.release()


def test_chat_failure_stays_visible_until_next_speech():
    def fail_chat(*_args):
        raise RuntimeError("private backend details")

    runtime = LiveVoiceRuntime(
        user_id="user",
        session_id="session",
        transcribe=lambda _audio: "hello",
        chat=fail_chat,
        synthesize=lambda *_: wav(pcm(1000)),
        source=FakePcmSource(),
        detector=EnergyTurnDetector(speech_start_seconds=0.04, endpoint_silence_seconds=0.1),
    )
    try:
        for at, amplitude in [(0, 2500), (0.04, 2500), (0.08, 2500), (0.12, 0), (0.23, 0)]:
            runtime.observe_pcm(pcm(amplitude), at=at)
        assert wait_for(lambda: runtime.last_error is not None)
        runtime.drain_events()
        assert runtime.status == "Voice error"
        assert runtime.last_error == "The chat service failed. Please try again."
        for at in (0.30, 0.35, 0.40):
            runtime.observe_pcm(pcm(2500), at=at)
        assert runtime.last_error is None
        assert runtime.status == "Listening to you"
    finally:
        runtime.release()


def test_new_turn_can_finish_while_old_buffered_chat_is_in_flight():
    chat_entered = threading.Event()
    unblock_chat = threading.Event()
    transcribe_count = 0
    count_lock = threading.Lock()
    source = FakePcmSource()

    def transcribe(_audio: bytes) -> str:
        nonlocal transcribe_count
        with count_lock:
            transcribe_count += 1
            return f"turn {transcribe_count}"

    def chat(_user_id: str, _session_id: str, text: str) -> str:
        if text == "turn 1":
            chat_entered.set()
            assert unblock_chat.wait(2.0)
            return "Old answer must be suppressed."
        return "Answer for the current turn."

    runtime = LiveVoiceRuntime(
        user_id="acct-1",
        session_id="session-1",
        transcribe=transcribe,
        chat=chat,
        synthesize=lambda _text, _language: wav(pcm(1000)),
        source=source,
        detector=EnergyTurnDetector(
            speech_start_seconds=0.04,
            endpoint_silence_seconds=0.06,
            energy_threshold=600,
        ),
    )
    try:
        for at, amplitude in [(0.0, 2500), (0.04, 2500), (0.08, 0), (0.15, 0)]:
            runtime.observe_pcm(pcm(amplitude), at=at)
        assert chat_entered.wait(1.0)
        first_turn = 1

        # The second bounded worker can process a new turn while the first
        # buffered chat call is in flight.
        for at, amplitude in [(0.20, 2500), (0.25, 2500), (0.29, 0), (0.36, 0)]:
            runtime.observe_pcm(pcm(amplitude), at=at)
        current_assistants: list = []

        def current_reply_arrived() -> bool:
            current_assistants.extend(
                event
                for event in runtime.drain_events()
                if event.kind == "assistant" and event.turn_id != first_turn
            )
            return bool(current_assistants)

        assert wait_for(current_reply_arrived)
        assert runtime.latest_reply == (first_turn + 1, "Answer for the current turn.")
        unblock_chat.set()

        stale_events: list = []

        def old_reply_suppressed() -> bool:
            stale_events.extend(
                event
                for event in runtime.drain_events()
                if event.kind == "stale_response" and event.turn_id == first_turn
            )
            return bool(stale_events)

        assert wait_for(old_reply_suppressed)
        assert source.pushed
    finally:
        unblock_chat.set()
        runtime.release()


def test_release_is_idempotent_and_rejects_later_audio():
    runtime = LiveVoiceRuntime(
        user_id="acct-1",
        session_id="session-1",
        transcribe=lambda _: "",
        chat=lambda *_: "",
        synthesize=lambda *_: b"",
        source=FakePcmSource(),
    )
    runtime.release()
    runtime.release()
    with pytest.raises(RuntimeError, match="released"):
        runtime.observe_pcm(pcm(1000), at=0.0)


def test_webrtc_uses_wrapper_track_and_keeps_audio_sendback_enabled():
    class SourceWrapper:
        track = object()

    source = SourceWrapper()
    sink = object()
    options = _webrtc_audio_options("opaque-owner-key", source, sink)

    assert options["source_audio_track"] is source.track
    assert options["sink_audio_track"] is sink
    assert options["sendback_audio"] is True
    assert options["translations"] == {
        "start": "Start talking",
        "stop": "End conversation",
    }
