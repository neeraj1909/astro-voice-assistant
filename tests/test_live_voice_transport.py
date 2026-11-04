"""Real local aiortc transport through streamlit-webrtc's installed worker.

Synthetic PCM is sent through an in-process peer pair. ASR, chat, and TTS are
deterministic local adapters; this proves media routing and turn plumbing, not
browser capture, acoustic recognition quality, or microphone playback.
"""

from __future__ import annotations

import asyncio
import io
import math
import threading
import time
import wave

import av
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import AudioStreamTrack
from streamlit_webrtc import PcmAudioSource
from streamlit_webrtc.sink import AudioSinkTrack
from streamlit_webrtc.webrtc import WebRtcMode, WebRtcWorker

from app.live_voice import EnergyTurnDetector, LiveVoiceRuntime


class SyntheticMicTrack(AudioStreamTrack):
    """Paced, non-sensitive 8-kHz tone/silence frames for a local peer test."""

    def __init__(self) -> None:
        super().__init__()
        self.level = 0

    async def recv(self) -> av.AudioFrame:
        frame = await super().recv()
        indexes = np.arange(frame.samples, dtype=np.float64) + int(frame.pts or 0)
        tone = self.level * np.sin(2 * math.pi * 440 * indexes / 8000)
        samples = tone.astype(np.int16).reshape(1, -1)
        result = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        result.sample_rate = 8000
        result.pts = frame.pts
        result.time_base = frame.time_base
        return result


class ObservedPcmSource:
    """Observe push/clear while retaining the installed streamlit-webrtc source."""

    def __init__(self) -> None:
        self.inner = PcmAudioSource(sample_rate=24_000, ptime=0.020)
        self.pushed = threading.Event()
        self.clear_count = 0

    @property
    def track(self):
        return self.inner.track

    def push(self, pcm: bytes) -> None:
        self.inner.push(pcm)
        self.pushed.set()

    def clear(self) -> None:
        self.clear_count += 1
        self.inner.clear()


def _tone_wav(seconds: float, *, rate: int = 24_000, amplitude: int = 5000) -> bytes:
    count = int(seconds * rate)
    samples = np.arange(count, dtype=np.float64)
    pcm = (amplitude * np.sin(2 * math.pi * 440 * samples / rate)).astype(np.int16)
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm.tobytes())
    return output.getvalue()


async def _wait_thread_event(event: threading.Event, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if event.is_set():
            return True
        await asyncio.sleep(0.02)
    return event.is_set()


async def _exercise_peer_transport() -> None:
    loop = asyncio.get_running_loop()
    source = ObservedPcmSource()
    seen_wavs: list[bytes] = []
    chat_calls: list[tuple[str, str, str]] = []
    tts_calls: list[tuple[str, str]] = []
    output_non_silent = asyncio.Event()

    def transcribe(wav_bytes: bytes) -> str:
        seen_wavs.append(wav_bytes)
        with wave.open(io.BytesIO(wav_bytes), "rb") as captured:
            assert (captured.getnchannels(), captured.getsampwidth(), captured.getframerate()) == (
                1,
                2,
                16_000,
            )
            assert captured.getnframes() > 0
        return "A synthetic voice asks about the stars."

    def chat(user_id: str, session_id: str, text: str) -> str:
        chat_calls.append((user_id, session_id, text))
        return "The synthetic stars reply locally."

    def synthesize(text: str, language: str) -> bytes:
        tts_calls.append((text, language))
        return _tone_wav(2.0)

    def receive_output(frame: av.AudioFrame) -> None:
        pcm = frame.to_ndarray().astype(np.int32)
        if pcm.size and int(np.max(np.abs(pcm))) > 120:
            loop.call_soon_threadsafe(output_non_silent.set)

    runtime = LiveVoiceRuntime(
        user_id="transport-test-user",
        session_id="transport-test-session",
        transcribe=transcribe,
        chat=chat,
        synthesize=synthesize,
        source=source,
        detector=EnergyTurnDetector(
            speech_start_seconds=0.10,
            endpoint_silence_seconds=0.28,
            energy_threshold=600,
        ),
    )

    mic = SyntheticMicTrack()
    caller = RTCPeerConnection()
    output_sink = AudioSinkTrack(callback=receive_output)
    worker = WebRtcWorker(
        mode=WebRtcMode.SENDRECV,
        rtc_configuration=None,
        source_video_track=None,
        source_audio_track=source.track,
        sink_video_track=None,
        sink_audio_track=AudioSinkTrack(callback=runtime.process_audio_frame),
        player_factory=None,
        in_recorder_factory=None,
        out_recorder_factory=None,
        video_frame_callback=None,
        audio_frame_callback=None,
        queued_video_frames_callback=None,
        queued_audio_frames_callback=None,
        on_video_ended=None,
        on_audio_ended=None,
        video_processor_factory=None,
        audio_processor_factory=None,
        async_processing=False,
        video_receiver_size=1,
        audio_receiver_size=1,
        sendback_video=False,
        sendback_audio=True,
        loop=loop,
        relay=MediaRelay(),
    )

    @caller.on("track")
    def on_return_track(track):
        if track.kind == "audio":
            output_sink.addTrack(track)
            output_sink.start()

    caller.addTrack(mic)

    try:
        offer = await caller.createOffer()
        await caller.setLocalDescription(offer)
        answer = await asyncio.to_thread(
            worker.process_offer,
            caller.localDescription.sdp,
            caller.localDescription.type,
            10.0,
        )
        assert isinstance(answer, RTCSessionDescription)
        await caller.setRemoteDescription(answer)

        # One voiced acoustic frame sequence followed by enough endpoint
        # silence to exercise the real installed sink and runtime resampler.
        mic.level = 4000
        await asyncio.sleep(0.38)
        mic.level = 0
        await asyncio.sleep(0.48)

        assert await _wait_thread_event(source.pushed), (
            "local TTS did not reach the WebRTC source; "
            f"transcribed={len(seen_wavs)}, chat={chat_calls}, "
            f"events={runtime.drain_events()}, peer={caller.connectionState}"
        )
        assert await asyncio.wait_for(output_non_silent.wait(), timeout=5.0), (
            "the return peer track produced no non-silent audio"
        )

        deadline = time.monotonic() + 5.0
        while not chat_calls and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert seen_wavs
        assert chat_calls == [
            (
                "transport-test-user",
                "transport-test-session",
                "A synthetic voice asks about the stars.",
            )
        ]
        assert tts_calls == [("The synthetic stars reply locally.", "en")]

        first_reply_turn = runtime.latest_reply[0] if runtime.latest_reply else None
        assert first_reply_turn is not None
        assert runtime.is_response_current(first_reply_turn)

        # Barge in with a second sustained synthetic waveform while the 2s
        # return clip is being transported. VAD interruption must clear the
        # queue and fence the first reply from later playback.
        mic.level = 4000
        await asyncio.sleep(0.30)
        mic.level = 0
        deadline = time.monotonic() + 3.0
        interrupted = False
        while time.monotonic() < deadline:
            events = runtime.drain_events()
            interrupted = interrupted or any(event.kind == "interrupted" for event in events)
            if interrupted:
                break
            await asyncio.sleep(0.02)
        assert interrupted
        assert source.clear_count >= 1
        assert not runtime.is_response_current(first_reply_turn)
    finally:
        runtime.release()
        output_sink.stop()
        mic.stop()
        await caller.close()
        await asyncio.to_thread(worker.stop, 2.0)


def test_aiortc_peer_transports_synthetic_turn_and_return_audio_with_barge_in() -> None:
    """Exercise streamlit-webrtc's actual worker with only synthetic local media."""
    asyncio.run(_exercise_peer_transport())
