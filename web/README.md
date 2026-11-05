# MyNaksh voice web client

This is a dependency-free browser client for the MyNaksh voice assistant. The gateway must serve this
directory at `/` and proxy/provide the same-origin cookie endpoints below; do
not expose provider credentials to this directory or the browser.

## Same-origin API contract

- `GET /api/auth/session` returns `{authenticated:boolean, username?:string}`.
- `POST /api/auth/login` and `/api/auth/register` accept
  `{username,password}` and establish an HTTP-only same-origin session cookie.
- `POST /api/auth/logout` clears the cookie.
- `POST /api/realtime/call` accepts an SDP offer as `application/sdp`, returns
  an SDP answer as `application/sdp`, and includes the server-owned call ID in
  `X-Call-ID`. The server binds the call to the cookie-authenticated account and
  uses its protected MyNaksh tool; the browser sends no account or session IDs.
- `DELETE /api/realtime/call/{call_id}` releases that call. The browser sends
  same-origin cookies on every request.

The client uses a WebRTC audio track and `oai-events` data channel. It consumes
standard OpenAI Realtime event names without sending provider keys or creating
provider sessions in the browser. The server must enforce tool identity,
barge-in/cancellation, usage limits and provider session cleanup.

## Checks

Run `npm test` from this directory. The tests use fake browser media and HTTP
responses; they make no provider calls and do not validate real microphone,
audio quality, or network traversal. The app deliberately shows no elapsed-time
counter and no language selector.
