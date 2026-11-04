import test from "node:test";
import assert from "node:assert/strict";
import { createBrowserSpeech } from "../speech.js";

test("unsupported browser reports speech input and output unavailable", () => {
  const speech = createBrowserSpeech({ windowObject: {} });
  assert.equal(speech.recognitionAvailable, false);
  assert.equal(speech.synthesisAvailable, false);
  assert.equal(speech.createRecognition(() => {}, () => {}), null);
  assert.equal(speech.speak("hello"), false);
});

test("recognition exposes interim/final transcript and permission error states", () => {
  class FakeRecognition { start() {} stop() {} }
  const windowObject = { SpeechRecognition: FakeRecognition, navigator: { language: "en-GB" } };
  const speech = createBrowserSpeech({ windowObject });
  const updates = [];
  const states = [];
  const recognition = speech.createRecognition((...args) => updates.push(args), (...args) => states.push(args));
  assert.equal(recognition.lang, "en-GB");
  assert.equal(recognition.continuous, false);
  assert.equal(recognition.interimResults, true);
  recognition.onstart();
  recognition.onresult({ results: [Object.assign([{ transcript: "hello " }], { isFinal: false }), Object.assign([{ transcript: "stars" }], { isFinal: true })] });
  recognition.onerror({ error: "not-allowed" });
  assert.deepEqual(updates, [["hello stars", true]]);
  assert.deepEqual(states[0], ["listening"]);
  assert.equal(states[1][0], "error");
  assert.match(states[1][1], /permission was denied/);
});

test("speech synthesis can be requested and cancelled", () => {
  const spoken = [];
  let cancelled = 0;
  class FakeUtterance { constructor(text) { this.text = text; } }
  const windowObject = {
    SpeechSynthesisUtterance: FakeUtterance,
    speechSynthesis: { cancel: () => { cancelled += 1; }, speak: (item) => spoken.push(item) },
  };
  const speech = createBrowserSpeech({ windowObject });
  assert.equal(speech.speak("hello", () => {}, () => {}), true);
  assert.equal(spoken[0].text, "hello");
  speech.stopSpeaking();
  assert.equal(cancelled, 2);
});
