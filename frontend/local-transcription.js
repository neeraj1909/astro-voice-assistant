import { MAX_WAV_BYTES } from "./local-wav.js";

export function createLocalTranscriber(fetchImpl = globalThis.fetch, maxBytes = MAX_WAV_BYTES) {
  return async function transcribeWav(blob, { signal } = {}) {
    if (!(blob instanceof Blob) || blob.type !== "audio/wav") throw new TypeError("A WAV recording is required.");
    if (blob.size < 44 || blob.size > maxBytes) throw new RangeError("The recording is empty or larger than the allowed limit.");
    let response;
    try {
      response = await fetchImpl("/api/transcribe", {
        method: "POST",
        headers: { "Content-Type": "audio/wav", Accept: "application/json" },
        body: blob,
        signal,
      });
    } catch {
      throw new Error("Could not reach local transcription. Your recording is still available to retry or discard.");
    }
    let data;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) throw new Error(data?.detail || data?.error || `Transcription failed (${response.status}).`);
    if (!data || typeof data.text !== "string" || !data.text.trim()) throw new Error("The transcription service returned an unreadable transcription.");
    return { text: data.text.trim() };
  };
}
