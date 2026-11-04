export const WAV_SAMPLE_RATE = 16000;
export const MAX_RECORDING_MS = 30_000;
export const MAX_WAV_BYTES = 960_044;

function joinSamples(chunks, maxFrames) {
  const frameCount = Math.min(chunks.reduce((sum, chunk) => sum + chunk.length, 0), maxFrames);
  const samples = new Float32Array(frameCount);
  let offset = 0;
  for (const chunk of chunks) {
    const count = Math.min(chunk.length, frameCount - offset);
    if (count <= 0) break;
    samples.set(chunk.subarray(0, count), offset);
    offset += count;
  }
  return samples;
}

async function resampleWithOfflineAudioContext(samples, sourceRate, OfflineAudioContextClass) {
  if (sourceRate === WAV_SAMPLE_RATE) return samples;
  if (typeof OfflineAudioContextClass !== "function") {
    throw new Error("This browser cannot safely resample microphone audio to 16 kHz. Use text or browser speech input instead.");
  }
  const outputFrames = Math.floor(samples.length * WAV_SAMPLE_RATE / sourceRate);
  if (outputFrames <= 0) return new Float32Array(0);
  const offline = new OfflineAudioContextClass(1, outputFrames, WAV_SAMPLE_RATE);
  const buffer = offline.createBuffer(1, samples.length, sourceRate);
  buffer.copyToChannel(samples, 0);
  const source = offline.createBufferSource();
  source.buffer = buffer;
  source.connect(offline.destination);
  source.start(0);
  const rendered = await offline.startRendering();
  return rendered.getChannelData(0);
}

export async function encodePcm16Wav(chunks, sourceRate, maxDurationSeconds = MAX_RECORDING_MS / 1000, OfflineAudioContextClass = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext) {
  if (!Number.isFinite(sourceRate) || sourceRate <= 0) throw new TypeError("A valid audio sample rate is required.");
  if (!Array.isArray(chunks) || chunks.some((chunk) => !(chunk instanceof Float32Array))) throw new TypeError("Audio samples must be Float32Array chunks.");
  const seconds = Math.min(MAX_RECORDING_MS / 1000, Math.max(0, Number(maxDurationSeconds) || 0));
  const samples = joinSamples(chunks, Math.floor(sourceRate * seconds));
  if (samples.length === 0) throw new Error("No audio was captured. Try recording again or type your message.");
  const rendered = await resampleWithOfflineAudioContext(samples, sourceRate, OfflineAudioContextClass);
  const pcm = new Int16Array(rendered.length);
  for (let i = 0; i < rendered.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, rendered[i]));
    pcm[i] = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
  }
  const buffer = new ArrayBuffer(44 + pcm.byteLength);
  const view = new DataView(buffer);
  const ascii = (offset, value) => { for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i)); };
  ascii(0, "RIFF"); view.setUint32(4, 36 + pcm.byteLength, true); ascii(8, "WAVE");
  ascii(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, WAV_SAMPLE_RATE, true);
  view.setUint32(28, WAV_SAMPLE_RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  ascii(36, "data"); view.setUint32(40, pcm.byteLength, true);
  for (let i = 0; i < pcm.length; i += 1) view.setInt16(44 + i * 2, pcm[i], true);
  return new Blob([buffer], { type: "audio/wav" });
}

export function createLocalWavRecorder({
  mediaDevices = globalThis.navigator?.mediaDevices,
  AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext,
  OfflineAudioContextClass = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext,
  maxDurationMs = MAX_RECORDING_MS,
  maxBytes = MAX_WAV_BYTES,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
} = {}) {
  maxDurationMs = Math.min(MAX_RECORDING_MS, Math.max(1, Number(maxDurationMs) || MAX_RECORDING_MS));
  let stream = null;
  let context = null;
  let source = null;
  let processor = null;
  let chunks = [];
  let capturedFrames = 0;
  let recording = false;
  let stopTimer = null;
  let stopping = null;
  let lastBlob = null;
  let limitCallback = () => {};
  let errorCallback = () => {};
  let lifecycle = 0;

  async function release() {
    if (stopTimer !== null) { clearTimer(stopTimer); stopTimer = null; }
    if (processor) { processor.onaudioprocess = null; try { processor.disconnect(); } catch {} processor = null; }
    if (source) { try { source.disconnect(); } catch {} source = null; }
    for (const track of stream?.getTracks?.() ?? []) { try { track.stop(); } catch {} }
    stream = null;
    const activeContext = context;
    context = null;
    if (activeContext && activeContext.state !== "closed") { try { await activeContext.close(); } catch {} }
  }

  async function finish(keepAudio) {
    if (stopping) return stopping;
    if (!recording && !stream && !context) return keepAudio ? lastBlob : null;
    recording = false;
    stopping = (async () => {
      await release();
      try {
        const blob = keepAudio ? await encodePcm16Wav(chunks, sourceRate || 16000, maxDurationMs / 1000, OfflineAudioContextClass) : null;
        if (blob && blob.size > maxBytes) throw new RangeError("The recording reached the size limit. Try a shorter message.");
        lastBlob = blob;
        return blob;
      } finally {
        chunks = [];
        capturedFrames = 0;
        stopping = null;
      }
    })();
    return stopping;
  }

  let sourceRate = 16000;
  const recorder = {
    get supported() { return Boolean(mediaDevices?.getUserMedia && AudioContextClass); },
    get recording() { return recording; },
    async start(onLimit = () => {}, onError = () => {}) {
      if (!recorder.supported) throw new Error("Local audio recording is not supported in this browser or context.");
      if (recording || stream || context) throw new Error("A recording is already active.");
      chunks = []; capturedFrames = 0; lastBlob = null; stopping = null; limitCallback = onLimit; errorCallback = onError;
      const startGeneration = ++lifecycle;
      try {
        const acquiredStream = await mediaDevices.getUserMedia({ audio: { channelCount: 1 }, video: false });
        if (startGeneration !== lifecycle) {
          for (const track of acquiredStream.getTracks?.() ?? []) track.stop();
          return;
        }
        stream = acquiredStream;
        context = new AudioContextClass();
        if (context.state === "suspended") await context.resume();
        if (startGeneration !== lifecycle) throw new Error("Recording start was cancelled.");
        sourceRate = context.sampleRate;
        if (!Number.isFinite(sourceRate) || sourceRate <= 0) throw new Error("Audio device reported an invalid sample rate.");
        if (sourceRate !== WAV_SAMPLE_RATE && typeof OfflineAudioContextClass !== "function") {
          throw new Error("This browser cannot safely resample microphone audio to 16 kHz. Use text or browser speech input instead.");
        }
        source = context.createMediaStreamSource(stream);
        processor = context.createScriptProcessor(4096, 1, 1);
        const maxFrames = Math.floor(sourceRate * maxDurationMs / 1000);
        processor.onaudioprocess = (event) => {
          if (!recording) return;
          const input = event.inputBuffer.getChannelData(0);
          const count = Math.min(input.length, maxFrames - capturedFrames);
          if (count > 0) { chunks.push(new Float32Array(input.subarray(0, count))); capturedFrames += count; }
          event.outputBuffer?.getChannelData?.(0)?.fill(0);
          if (capturedFrames >= maxFrames) {
            void finish(true).then((blob) => limitCallback(blob)).catch((error) => errorCallback(error));
          }
        };
        source.connect(processor);
        processor.connect(context.destination);
        recording = true;
        stopTimer = setTimer(() => {
          if (recording) void finish(true).then((blob) => limitCallback(blob)).catch((error) => errorCallback(error));
        }, maxDurationMs);
      } catch (error) {
        recording = false;
        lifecycle += 1;
        await release();
        chunks = [];
        throw error?.name === "NotAllowedError" || error?.name === "PermissionDeniedError"
          ? new Error("Microphone permission was denied. You can still type or use browser speech input.")
          : new Error(error?.message || "Could not start local audio recording.");
      }
    },
    stop() { return finish(true); },
    async abort() {
      lifecycle += 1;
      recording = false;
      await release();
      chunks = []; capturedFrames = 0; lastBlob = null; stopping = null;
    },
    maxBytes,
  };
  return recorder;
}
