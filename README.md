# Starlight local astrology chat

The supported UI is the Streamlit app in `streamlit_app.py`. FastAPI (`app.main`) provides a loopback gateway to an existing assignment chat endpoint and optional local Whisper.cpp transcription. Older vanilla frontend files remain in the repository but are not served by the gateway; use Streamlit as the chat UI.

## Run locally

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Replace the example `CHAT_API_URL` with the full, authorized assignment endpoint. This one-shell example gives both local services the same random server-only token and binds them to loopback:

```sh
uv sync
export ASTRO_GATEWAY_TOKEN="$(uv run python -c 'import secrets; print(secrets.token_hex(32))')"
export CHAT_API_URL="https://<assignment-host>/<chat-path>"
export GATEWAY_URL="http://127.0.0.1:8000"

# Optional local transcription settings; see below.
export WHISPER_CLI="/path/to/whisper.cpp/build/bin/whisper-cli"
export WHISPER_MODEL="/path/to/whisper.cpp/models/ggml-small.bin"
export WHISPER_LANGUAGE="auto"

uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 &
gateway_pid=$!
trap 'kill "$gateway_pid" 2>/dev/null || true' EXIT
uv run streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

Open <http://127.0.0.1:8501>, create a local account, and sign in. Keep both services on loopback; do not expose them to untrusted users or the public internet. Both `/api/chat` and `/api/transcribe` require the same `ASTRO_GATEWAY_TOKEN`, at least 32 ASCII non-whitespace characters. It is sent server-to-server as a bearer token and is not included in the chat JSON payload.

`CHAT_API_URL` must accept JSON containing `user_id`, `session_id`, and `message`, then return `request_id`, integer `message_id` and `response_id`, matching `user_id` and `session_id`, string `response`, and string-array `context_used`. The gateway does not configure upstream authorization headers or credentials. If the assignment endpoint requires separate authentication, this gateway needs an authorized integration before it can use that endpoint; do not put credentials in the URL. Upstream calls may incur costs according to the assignment provider's account and terms.

The local SQLite account database defaults to `~/.local/share/conversational-ai-for-astro/accounts.sqlite3`; set `ASTRO_ACCOUNTS_DB` to override it. Passwords are stored as salted scrypt hashes. The local account screen gates the Streamlit UI, but the gateway token is a shared service credential, not an individual user identity. Account/session isolation and upstream memory behavior are not verified. Signing out clears the current Streamlit session. **New conversation** clears the displayed history and rotates the session ID but does not delete history held upstream.

## Experimental live voice

This is a local prototype built with `streamlit-webrtc`, not a production voice assistant. After you consent to live microphone streaming and automatic spoken replies, WebRTC sends audio frames to this app. A simple RMS-energy VAD segments speech using onset and silence thresholds; each completed turn is buffered and sent to the configured local transcription endpoint, then its transcript is sent to the chat endpoint. Replies are synthesized locally and played through the browser audio connection. The microphone remains active during playback; sustained speech attempts to stop queued playback and start a new turn.

Live voice depends on browser/device microphone permissions, audio routing, microphone gain, and background noise. The VAD can miss speech or trigger on noise, so turn timing and barge-in are best-effort. This is not guaranteed full-duplex behavior. The local voice selector controls **read-aloud output only** (`en`, `hi`, or `bn`); it does not select the Whisper input language or provide code-switch recognition. Calls already started for transcription or chat cannot be canceled; late replies/audio are suppressed locally, but an upstream conversation update may already have occurred. Live upstream chat and local synthesis can add noticeable latency.

## Local speech dependencies

For transcription, install whisper.cpp and download a compatible model separately. Set `WHISPER_CLI` to its executable and `WHISPER_MODEL` to the model file. `WHISPER_LANGUAGE` defaults to `en`; it may be `auto` or a 2–3 letter language code such as `hi` or `bn`. Use a multilingual model for any language other than English (for example, `ggml-small.bin`, not an English-only `.en` model). The server accepts mono, 16 kHz, 16-bit PCM WAV up to 30 seconds and 8 MiB. Whisper runs locally, has a 60-second timeout, and uses a temporary directory that is deleted after processing. Actual recognition accuracy and latency depend on the model and hardware.

For spoken replies, install the eSpeak-NG shared library (`libespeak-ng.so.1`) and the voice data for the desired languages on the host. Synthesis runs in a bounded local worker, returns mono PCM16 WAV, and supports English, Hindi, and Bengali output voices. It has a 15-second timeout and limits reply text to 500 characters; pronunciation quality is not guaranteed. No paid speech provider is used by these local paths. The separate **Record a voice message** control is buffered: it also requires consent and an explicit **Transcribe recording** action, and the resulting transcript still requires **Send**.

## Tests and evidence limits

```sh
uv run pytest
```

Tests cover account and gateway contracts, local VAD/turn-state behavior, stale-result handling, TTS bounds, and synthetic audio transport with controlled dependencies. They do not provide acoustic evidence from a physical microphone or headed browser, validate real Whisper transcription quality, prove pronunciation quality, or validate the configured assignment model's behavior. No actual astrology-only/domain policy is enforced: arbitrary user messages are forwarded to the configured upstream.

Use only for local trusted testing. The local account screen does not protect direct API callers who obtain the shared gateway token, and this project does not guarantee account isolation or provider-side memory isolation. Check the upstream's privacy and billing behavior before sending data; model calls may be billable.
