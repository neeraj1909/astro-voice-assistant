import test from "node:test";
import assert from "node:assert/strict";
import { createLocalTranscriber } from "../local-transcription.js";
import { createLocalTranscriptionFlow } from "../local-transcription-flow.js";
import { encodePcm16Wav, createLocalWavRecorder, MAX_RECORDING_MS, MAX_WAV_BYTES } from "../local-wav.js";

test("frontend recording bounds match the 30-second backend contract", () => {
  assert.equal(MAX_RECORDING_MS, 30_000);
  assert.equal(MAX_WAV_BYTES, 960_044);
});

test("WAV output is bounded mono PCM16 at 16 kHz", async () => {
  const source = new Float32Array(48000);
  source[0] = -1;
  source[24000] = 1;
  let offlineArgs;
  class FakeOfflineAudioContext {
    constructor(channels, length, sampleRate) { offlineArgs = [channels, length, sampleRate]; this.length = length; this.destination = {}; }
    createBuffer(channels, length, sampleRate) {
      const buffer = { channels, length, sampleRate, samples: null, copyToChannel(samples) { this.samples = samples; } };
      this.buffer = buffer;
      return buffer;
    }
    createBufferSource() { return { connect() {}, start() {} }; }
    async startRendering() { return { getChannelData: () => new Float32Array(this.length) }; }
  }
  const wav = await encodePcm16Wav([source], 48000, 2, FakeOfflineAudioContext);
  assert.equal(wav.type, "audio/wav");
  assert.deepEqual(offlineArgs, [1, 16000, 16000]);
  const bytes = new DataView(await wav.arrayBuffer());
  assert.equal(bytes.getUint32(24, true), 16000);
  assert.equal(bytes.getUint16(22, true), 1);
  assert.equal(bytes.getUint16(34, true), 16);
  assert.equal(bytes.getUint32(40, true), 32000);
  assert.equal(wav.size, 32044);
});

test("recorder releases tracks/context and stops at the configured capture bound", async () => {
  const resources = { stoppedTracks: 0, closedContexts: 0, processor: null };
  const stream = { getTracks: () => [{ stop: () => { resources.stoppedTracks += 1; } }] };
  class FakeAudioContext {
    sampleRate = 16000;
    destination = {};
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createScriptProcessor() { resources.processor = { connect() {}, disconnect() {}, onaudioprocess: null }; return resources.processor; }
    async close() { resources.closedContexts += 1; }
  }
  const recorder = createLocalWavRecorder({
    mediaDevices: { getUserMedia: async () => stream },
    AudioContextClass: FakeAudioContext,
    maxDurationMs: 1000,
  });
  let onLimit;
  const limitReached = new Promise((resolve) => { onLimit = resolve; });
  await recorder.start(onLimit);
  const source = new Float32Array(20000);
  resources.processor.onaudioprocess({ inputBuffer: { getChannelData: () => source }, outputBuffer: { getChannelData: () => new Float32Array(4096) } });
  const wav = await limitReached;
  assert.equal(wav.size, 32044);
  assert.equal(resources.stoppedTracks, 1);
  assert.equal(resources.closedContexts, 1);
});

test("consent and a separate Transcribe action are required before upload", async () => {
  let uploads = 0;
  let transcript = "";
  const recorder = { start: async () => {}, stop: async () => new Blob(["wav"], { type: "audio/wav" }), abort: async () => {} };
  const flow = createLocalTranscriptionFlow({
    recorder,
    transcribe: async () => { uploads += 1; return { text: "Hello stars" }; },
    onTranscript: (text) => { transcript = text; },
  });
  await assert.rejects(flow.start(false), /consent/i);
  assert.equal(uploads, 0);
  await flow.start(true);
  await flow.stop();
  assert.equal(uploads, 0);
  await flow.transcribe();
  assert.equal(uploads, 1);
  assert.equal(transcript, "Hello stars");
});

test("automatic capture stop reports the configured backend duration", async () => {
  let onLimit;
  const states = [];
  const recorder = {
    start: async (callback) => { onLimit = callback; },
    stop: async () => new Blob(["wav"], { type: "audio/wav" }),
    abort: async () => {},
  };
  const flow = createLocalTranscriptionFlow({ recorder, transcribe: async () => ({ text: "ok" }), onState: (value) => states.push(value) });
  await flow.start(true);
  onLimit(new Blob(["wav"], { type: "audio/wav" }));
  assert.equal(flow.stage, "ready");
  assert.match(states.at(-1).detail, /30-second recording limit/);
});

test("discard makes an in-flight transcription result stale", async () => {
  let resolveTranscription;
  let transcript = "";
  let requestSignal;
  const recorder = { start: async () => {}, stop: async () => new Blob(["wav"], { type: "audio/wav" }), abort: async () => {} };
  const flow = createLocalTranscriptionFlow({
    recorder,
    transcribe: (_blob, { signal }) => { requestSignal = signal; return new Promise((resolve) => { resolveTranscription = resolve; }); },
    onTranscript: (text) => { transcript = text; },
  });
  await flow.start(true);
  await flow.stop();
  const pending = flow.transcribe();
  await flow.discard();
  assert.equal(requestSignal.aborted, true);
  resolveTranscription({ text: "stale" });
  await pending;
  assert.equal(transcript, "");
});

test("microphone denial reports a recoverable error without retaining tracks", async () => {
  const denied = Object.assign(new Error("blocked"), { name: "NotAllowedError" });
  const recorder = createLocalWavRecorder({ mediaDevices: { getUserMedia: async () => { throw denied; } }, AudioContextClass: class {} });
  await assert.rejects(recorder.start(), /permission was denied/);
  assert.equal(recorder.recording, false);
});

test("unsupported resampling fails closed and releases microphone resources", async () => {
  let stopped = 0;
  let closed = 0;
  class FakeAudioContext {
    sampleRate = 48000;
    async close() { closed += 1; }
  }
  const recorder = createLocalWavRecorder({
    mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => { stopped += 1; } }] }) },
    AudioContextClass: FakeAudioContext,
    OfflineAudioContextClass: null,
  });
  await assert.rejects(recorder.start(), /cannot safely resample/);
  assert.equal(stopped, 1);
  assert.equal(closed, 1);
});

test("cancel while microphone permission is pending releases the late stream", async () => {
  let resolveStream;
  let stopped = 0;
  const recorder = createLocalWavRecorder({
    mediaDevices: { getUserMedia: () => new Promise((resolve) => { resolveStream = resolve; }) },
    AudioContextClass: class {},
  });
  const starting = recorder.start();
  await Promise.resolve();
  await recorder.abort();
  resolveStream({ getTracks: () => [{ stop: () => { stopped += 1; } }] });
  await starting;
  assert.equal(stopped, 1);
  assert.equal(recorder.recording, false);
});

test("transcriber sends raw WAV only when called and handles malformed response", async (t) => {
  let request;
  const transcribe = createLocalTranscriber(async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ text: "Recognized words" }) };
  });
  const wav = new Blob([new Uint8Array(48)], { type: "audio/wav" });
  const result = await transcribe(wav);
  assert.equal(request.url, "/api/transcribe");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers["Content-Type"], "audio/wav");
  assert.equal(request.options.body, wav);
  assert.equal(result.text, "Recognized words");
  await t.test("propagates caller cancellation to fetch", async () => {
    let signal;
    const controller = new AbortController();
    const withSignal = createLocalTranscriber(async (_url, options) => {
      signal = options.signal;
      return { ok: true, json: async () => ({ text: "ok" }) };
    });
    await withSignal(wav, { signal: controller.signal });
    assert.equal(signal, controller.signal);
  });
  await t.test("malformed JSON contract", async () => {
    const bad = createLocalTranscriber(async () => ({ ok: true, json: async () => ({ text: "" }) }));
    await assert.rejects(bad(wav), /unreadable transcription/);
  });
  await t.test("oversized audio never reaches fetch", async () => {
    let called = false;
    const bounded = createLocalTranscriber(async () => { called = true; return { ok: true, json: async () => ({ text: "" }) }; });
    const oversized = new Blob([new Uint8Array(MAX_WAV_BYTES + 1)], { type: "audio/wav" });
    await assert.rejects(bounded(oversized), /larger than the allowed limit/);
    assert.equal(called, false);
  });
});
