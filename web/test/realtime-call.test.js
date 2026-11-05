import test from 'node:test';
import assert from 'node:assert/strict';
import { PlaybackGate, RealtimeCall } from '../realtime-call.js';

test('interruption mutes stale audio until the next response starts playing', () => {
  const audio = { muted: false };
  const playback = new PlaybackGate(audio);
  playback.receive({ type: 'response.created', response: { id: 'old' } });
  playback.receive({ type: 'input_audio_buffer.speech_started' });
  assert.equal(audio.muted, true);
  playback.receive({ type: 'response.created', response: { id: 'tool-call' } });
  assert.equal(audio.muted, true);
  playback.receive({ type: 'output_audio_buffer.started', response_id: 'old' });
  assert.equal(audio.muted, true);
  playback.receive({ type: 'input_audio_buffer.speech_stopped' });
  playback.receive({ type: 'response.created', response: { id: 'new' } });
  playback.receive({ type: 'output_audio_buffer.started', response_id: 'new' });
  assert.equal(audio.muted, false);
});

class Track {
  constructor() { this.stopped = false; }
  stop() { this.stopped = true; }
}

class Stream {
  constructor() { this.track = new Track(); }
  getTracks() { return [this.track]; }
  getAudioTracks() { return [this.track]; }
}

class Channel {
  constructor(label) { this.label = label; this.readyState = 'connecting'; this.closed = false; }
  close() { this.closed = true; this.readyState = 'closed'; }
}

class PeerConnection {
  constructor() {
    this.connectionState = 'new';
    this.iceGatheringState = 'complete';
    this.channels = [];
    this.tracks = [];
    this.closed = false;
    this.remoteSet = false;
  }
  addTrack(track, stream) { this.tracks.push({ track, stream }); }
  createDataChannel(label) { const channel = new Channel(label); this.channels.push(channel); return channel; }
  async createOffer() { return { type: 'offer', sdp: 'offer-sdp' }; }
  async setLocalDescription(value) { this.localDescription = value; }
  async setRemoteDescription(value) { this.remoteDescription = value; this.remoteSet = true; this.connectionState = 'connected'; this.onconnectionstatechange?.(); }
  close() { this.closed = true; this.connectionState = 'closed'; }
}

function setup(fetchImpl) {
  const stream = new Stream();
  const peers = [];
  const statuses = [];
  const events = [];
  const errors = [];
  const call = new RealtimeCall({
    mediaDevices: { getUserMedia: async () => stream },
    RTCPeerConnection: class extends PeerConnection { constructor(...args) { super(...args); peers.push(this); } },
    MediaStream: class { constructor(tracks) { this.tracks = tracks; } },
    fetchImpl,
    onStatus: (status) => statuses.push(status),
    onEvent: (event) => events.push(event),
    onError: (error) => errors.push(error),
  });
  return { call, stream, peers, statuses, events, errors };
}

test('starts one same-origin call using SDP only and closes it by server-issued call id', async () => {
  const requests = [];
  const fake = setup(async (url, options) => {
    requests.push({ url, options });
    if (url === '/api/realtime/call' && options.method === 'POST') {
      return new Response('answer-sdp', { status: 200, headers: { 'Content-Type': 'application/sdp', 'X-Call-ID': 'call-abc' } });
    }
    return new Response(null, { status: 204 });
  });

  await fake.call.start();
  assert.equal(fake.peers.length, 1);
  assert.equal(fake.peers[0].channels[0].label, 'oai-events');
  assert.equal(fake.peers[0].remoteDescription.sdp, 'answer-sdp');
  assert.equal(requests[0].url, '/api/realtime/call');
  assert.equal(requests[0].options.method, 'POST');
  assert.equal(requests[0].options.credentials, 'same-origin');
  assert.equal(requests[0].options.headers['Content-Type'], 'application/sdp');
  assert.equal(requests[0].options.body, 'offer-sdp');
  assert.equal('Authorization' in requests[0].options.headers, false);
  assert.equal(requests[0].options.body.includes('user_id'), false);
  assert.equal(requests[1].url, '/api/realtime/call/call-abc/ready');
  assert.equal(requests[1].options.method, 'POST');
  assert.equal(requests[1].options.credentials, 'same-origin');
  assert.equal(fake.statuses[0], 'connecting');
  assert.equal(fake.statuses.includes('listening'), true);

  await fake.call.end();
  assert.equal(requests[2].url, '/api/realtime/call/call-abc');
  assert.equal(requests[2].options.method, 'DELETE');
  assert.equal(requests[2].options.credentials, 'same-origin');
  assert.equal(fake.stream.track.stopped, true);
  assert.equal(fake.peers[0].channels[0].closed, true);
  assert.equal(fake.peers[0].closed, true);
  assert.equal(fake.statuses.at(-1), 'ready');
});

test('forwards current provider events and ignores data-channel messages after End', async () => {
  const fake = setup(async (url, options) => url === '/api/realtime/call' && options.method === 'POST'
    ? new Response('answer', { status: 200, headers: { 'X-Call-ID': 'call-1' } })
    : new Response(null, { status: 204 }));
  await fake.call.start();
  const channel = fake.peers[0].channels[0];
  const sendMessage = channel.onmessage;
  sendMessage({ data: JSON.stringify({ type: 'input_audio_buffer.speech_started' }) });
  assert.equal(fake.events.length, 1);
  assert.equal(fake.events[0].type, 'input_audio_buffer.speech_started');
  await fake.call.end();
  sendMessage({ data: JSON.stringify({ type: 'response.output_audio_transcript.done', transcript: 'stale' }) });
  assert.equal(fake.events.length, 1);
});

test('End during SDP negotiation deletes the late server call and never sets its answer', async () => {
  let resolvePost;
  const requests = [];
  const fake = setup((url, options) => {
    requests.push({ url, options });
    if (url === '/api/realtime/call' && options.method === 'POST') return new Promise((resolve) => { resolvePost = resolve; });
    return Promise.resolve(new Response(null, { status: 204 }));
  });

  const starting = fake.call.start();
  while (!resolvePost) await new Promise((resolve) => setImmediate(resolve));
  let endDone = false;
  const ending = fake.call.end().then(() => { endDone = true; });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(endDone, false);
  resolvePost(new Response('late-answer', { status: 200, headers: { 'X-Call-ID': 'late-call' } }));
  await Promise.all([starting, ending]);

  assert.equal(fake.peers[0].remoteSet, false);
  assert.equal(fake.stream.track.stopped, true);
  assert.equal(requests.some(({ url, options }) => url.endsWith('/late-call') && options.method === 'DELETE'), true);
});

test('sign-out waits for delayed offer and server deletion before allowing cookie revocation', async () => {
  let resolveOffer;
  let resolveDelete;
  let signalDelete;
  const requests = [];
  const deleteSeen = new Promise((resolve) => { signalDelete = resolve; });
  const fake = setup((url, options) => {
    requests.push({ url, options });
    if (url === '/api/realtime/call' && options.method === 'POST') {
      return new Promise((resolve) => { resolveOffer = resolve; });
    }
    if (options.method === 'DELETE') {
      return new Promise((resolve) => { resolveDelete = resolve; signalDelete(); });
    }
    return Promise.resolve(new Response(null, { status: 204 }));
  });
  const starting = fake.call.start();
  while (!resolveOffer) await new Promise((resolve) => setImmediate(resolve));
  let loggedOut = false;
  const signOut = (async () => {
    await fake.call.end();
    loggedOut = true;
  })();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(loggedOut, false);
  resolveOffer(new Response('late-answer', { status: 200, headers: { 'X-Call-ID': 'logout-race-call' } }));
  await deleteSeen;
  assert.equal(loggedOut, false);
  resolveDelete(new Response(null, { status: 204 }));
  await Promise.all([starting, signOut]);
  assert.equal(loggedOut, true);
  assert.equal(requests.at(-1).url, '/api/realtime/call/logout-race-call');
});

test('ready handshake failure is visible and tears down the server call', async () => {
  const requests = [];
  const fake = setup(async (url, options) => {
    requests.push({ url, options });
    if (url === '/api/realtime/call') return new Response('answer', { status: 200, headers: { 'X-Call-ID': 'not-ready' } });
    if (url.endsWith('/ready')) return new Response('unavailable', { status: 503 });
    return new Response(null, { status: 204 });
  });
  await assert.rejects(fake.call.start(), /unavailable/i);
  assert.equal(requests.some(({ url }) => url.endsWith('/not-ready/ready')), true);
  assert.equal(requests.filter(({ url, options }) => url.endsWith('/not-ready') && options.method === 'DELETE').length, 1);
  assert.equal(fake.statuses.includes('listening'), false);
  assert.equal(fake.stream.track.stopped, true);
});

test('a failed start cleanup retains the call for End retry and blocks a new start', async () => {
  let permitDelete = false;
  const fake = setup(async (url, options) => {
    if (url === '/api/realtime/call') return new Response('answer', { status: 200, headers: { 'X-Call-ID': 'retry-call' } });
    if (url.endsWith('/ready')) return new Response('unavailable', { status: 503 });
    if (options.method === 'DELETE') return new Response(null, { status: permitDelete ? 204 : 503 });
    throw new Error('unexpected request');
  });
  await assert.rejects(fake.call.start());
  assert.equal(fake.call.callId, 'retry-call');
  await assert.rejects(fake.call.start(), /already active/i);
  permitDelete = true;
  await fake.call.end();
  assert.equal(fake.call.callId, null);
});

test('surfaces unauthorized session response without provider fallback', async () => {
  const fake = setup(async () => new Response('sign in required', { status: 401 }));
  await assert.rejects(fake.call.start(), /sign in/i);
  assert.equal(fake.peers.length, 1);
  assert.equal(fake.peers[0].remoteSet, false);
  assert.match(fake.errors[0].message, /sign in/i);
  assert.equal(fake.statuses.includes('listening'), false);
});

test('microphone permission denial has an explicit recovery message', async () => {
  const fake = setup(async () => assert.fail('must not post SDP without microphone permission'));
  fake.call.mediaDevices.getUserMedia = async () => {
    const error = new Error('browser private detail');
    error.name = 'NotAllowedError';
    throw error;
  };
  await assert.rejects(fake.call.start(), /Microphone permission was denied/);
  assert.equal(fake.peers.length, 0);
  assert.equal(fake.errors[0].message.includes('browser private detail'), false);
});

test('makes ICE interruption visible and reflects recovery when the peer reconnects', async () => {
  const fake = setup(async (_url, options) => options.method === 'POST'
    ? new Response('answer', { status: 200, headers: { 'X-Call-ID': 'call-net' } })
    : new Response(null, { status: 204 }));
  await fake.call.start();
  const peer = fake.peers[0];
  peer.connectionState = 'disconnected';
  peer.onconnectionstatechange();
  assert.match(fake.errors.at(-1).message, /connection was interrupted/i);
  peer.connectionState = 'connected';
  peer.onconnectionstatechange();
  assert.equal(fake.statuses.at(-1), 'listening');
  await fake.call.end();
});

test('a delayed readiness response does not report listening while disconnected', async () => {
  let resolveReady;
  const fake = setup(async (url) => {
    if (url === '/api/realtime/call') return new Response('answer', { status: 200, headers: { 'X-Call-ID': 'call-delayed-ready' } });
    if (url.endsWith('/ready')) return new Promise((resolve) => { resolveReady = resolve; });
    return new Response(null, { status: 204 });
  });
  const starting = fake.call.start();
  while (!resolveReady) await new Promise((resolve) => setImmediate(resolve));
  const peer = fake.peers[0];
  peer.connectionState = 'disconnected';
  peer.onconnectionstatechange();
  resolveReady(new Response(null, { status: 204 }));
  await starting;
  assert.equal(fake.statuses.at(-1), 'connecting');
  peer.connectionState = 'connected';
  peer.onconnectionstatechange();
  assert.equal(fake.statuses.at(-1), 'listening');
  await fake.call.end();
});

test('failed ICE connection closes media and releases server call', async () => {
  const requests = [];
  const fake = setup(async (url, options) => {
    requests.push({ url, options });
    return options.method === 'POST'
      ? new Response('answer', { status: 200, headers: { 'X-Call-ID': 'call-failed' } })
      : new Response(null, { status: 204 });
  });
  await fake.call.start();
  fake.peers[0].connectionState = 'failed';
  fake.peers[0].onconnectionstatechange();
  for (let i = 0; i < 10 && fake.statuses.at(-1) !== 'error'; i += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(fake.call.peer, null);
  assert.equal(fake.stream.track.stopped, true);
  assert.equal(requests.some(({ url, options }) => url.endsWith('/call-failed') && options.method === 'DELETE'), true);
  assert.equal(fake.statuses.at(-1), 'error');
});
