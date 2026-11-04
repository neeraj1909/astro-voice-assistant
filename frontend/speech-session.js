export function createSpeechTurnGuard() {
  let generation = 0;
  return {
    begin() { generation += 1; return generation; },
    invalidate() { generation += 1; },
    isCurrent(turn) { return turn === generation; },
  };
}

export function applyTranscript(input, transcript) {
  const maxLength = Number.isInteger(input.maxLength) && input.maxLength >= 0 ? input.maxLength : Infinity;
  const raw = String(transcript);
  const value = raw.slice(0, maxLength);
  input.value = value;
  return { value, truncated: value.length < raw.length };
}
