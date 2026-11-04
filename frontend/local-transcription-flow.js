import { createSpeechTurnGuard } from "./speech-session.js";
import { MAX_RECORDING_MS } from "./local-wav.js";

export function createLocalTranscriptionFlow({ recorder, transcribe, onTranscript = () => {}, onState = () => {} }) {
  const guard = createSpeechTurnGuard();
  let clip = null;
  let stage = "idle";
  let generation = 0;
  let activeController = null;

  function state(next, detail = "") {
    stage = next;
    onState({ stage: next, detail });
  }

  function saveClip(blob, turn, limitReached = false) {
    if (!guard.isCurrent(turn) || !blob) return;
    clip = blob;
    state("ready", limitReached ? `The ${MAX_RECORDING_MS / 1000}-second recording limit was reached.` : "");
  }

  return {
    get stage() { return stage; },
    get hasClip() { return Boolean(clip); },
    async start(consented) {
      if (!consented) throw new Error("Consent is required before recording.");
      if (stage === "recording" || stage === "transcribing") throw new Error("Wait for the current audio task to finish.");
      await recorder.abort();
      clip = null;
      const turn = guard.begin();
      generation = turn;
      state("starting");
      try {
        await recorder.start(
          (blob) => saveClip(blob, turn, true),
          (error) => { if (guard.isCurrent(turn)) state("error", error.message); },
        );
        if (!guard.isCurrent(turn)) return;
        state("recording");
      } catch (error) {
        if (guard.isCurrent(turn)) state("error", error.message);
        throw error;
      }
    },
    async stop() {
      if (stage !== "recording") return clip;
      const turn = generation;
      try {
        const blob = await recorder.stop();
        saveClip(blob, turn);
        return clip;
      } catch (error) {
        if (guard.isCurrent(turn)) state("error", error.message);
        throw error;
      }
    },
    async transcribe() {
      if (stage === "transcribing") throw new Error("Transcription is already in progress.");
      if (!clip) throw new Error("Record a message before choosing Transcribe.");
      const captured = clip;
      const turn = guard.begin();
      generation = turn;
      const controller = new AbortController();
      activeController = controller;
      state("transcribing");
      try {
        const result = await transcribe(captured, { signal: controller.signal });
        if (!guard.isCurrent(turn)) return null;
        if (!result || typeof result.text !== "string" || !result.text.trim()) throw new Error("No transcript was returned. You can retry or type your message.");
        clip = null;
        activeController = null;
        state("complete");
        onTranscript(result.text.trim());
        return result.text.trim();
      } catch (error) {
        if (!guard.isCurrent(turn)) return null;
        activeController = null;
        state("ready", error.message);
        throw error;
      }
    },
    async discard() {
      const wasTranscribing = stage === "transcribing";
      guard.invalidate();
      generation = 0;
      activeController?.abort();
      activeController = null;
      clip = null;
      await recorder.abort();
      state("idle", wasTranscribing ? "Browser request cancelled where possible. The transcription service may continue processing and removes its temporary audio when processing finishes." : "");
    },
    async dispose() {
      guard.invalidate();
      activeController?.abort();
      activeController = null;
      clip = null;
      await recorder.abort();
      state("idle");
    },
  };
}
