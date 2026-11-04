import test from "node:test";
import assert from "node:assert/strict";
import { applyTranscript, createSpeechTurnGuard } from "../speech-session.js";

test("late recognition callbacks are ignored after stop and submit", () => {
  const guard = createSpeechTurnGuard();
  const accepted = [];
  const beginTurn = () => {
    const turn = guard.begin();
    return (transcript) => { if (guard.isCurrent(turn)) accepted.push(transcript); };
  };

  const stoppedTurnResult = beginTurn();
  stoppedTurnResult("before stop");
  guard.invalidate(); // User pressed Stop listening.
  stoppedTurnResult("late stop result");

  const submittedTurnResult = beginTurn();
  submittedTurnResult("before submit");
  guard.invalidate(); // User submitted the draft.
  submittedTurnResult("late submit result");
  assert.deepEqual(accepted, ["before stop", "before submit"]);
});

test("recognized text is clipped to the textarea limit and reports truncation", () => {
  const input = { maxLength: 5, value: "" };
  const result = applyTranscript(input, "starlight");
  assert.equal(input.value, "starl");
  assert.deepEqual(result, { value: "starl", truncated: true });
  assert.deepEqual(applyTranscript(input, "hello"), { value: "hello", truncated: false });
});
