import test from 'node:test';
import assert from 'node:assert/strict';
import { TranscriptModel } from '../transcript-model.js';

test('benign cancellation race does not present a voice failure', () => {
  const errors = [];
  const statuses = [];
  const model = new TranscriptModel({ onError: (e) => errors.push(e), onStatus: (s) => statuses.push(s) });
  model.receive({ type: 'error', error: { code: 'response_cancel_not_active' } });
  assert.equal(errors.length, 0);
  assert.equal(statuses.includes('error'), false);
  model.receive({ type: 'error', error: { code: 'server_error' } });
  assert.equal(errors.length, 1);
});

test('records final user transcript once and shows audio transcript, not tool text', () => {
  const model = new TranscriptModel();
  const speechStart = { type: 'input_audio_buffer.speech_started' };
  model.receive(speechStart);
  model.receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'input-1', transcript: 'What is in my stars?' });
  model.receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'input-1', transcript: 'duplicate' });
  model.receive({ type: 'response.created', response: { id: 'response-1' } });
  model.receive({ type: 'response.output_audio_transcript.delta', response_id: 'response-1', item_id: 'output-1', delta: 'Take a quiet ' });
  model.receive({ type: 'response.output_audio_transcript.delta', response_id: 'response-1', item_id: 'output-1', delta: 'moment.' });
  assert.deepEqual(model.messages.map(({ role, text }) => [role, text]), [
    ['user', 'What is in my stars?'],
    ['assistant', 'Take a quiet moment.'],
  ]);
  assert.equal(model.messages[1].complete, false);

  model.receive({ type: 'response.output_audio_transcript.done', response_id: 'response-1', item_id: 'output-1', transcript: 'Take a quiet moment.' });
  model.receive({ type: 'response.done', response: { id: 'response-1', output: [{ type: 'function_call', arguments: 'canonical answer is never shown' }] } });
  model.receive({ type: 'response.output_audio_transcript.done', response_id: 'response-1', item_id: 'output-1', transcript: 'duplicate done' });
  assert.equal(model.messages.length, 2);
  assert.equal(model.messages[1].complete, true);
  assert.equal(model.messages.some(({ text }) => text.includes('canonical answer')), false);
});

test('marks interrupted speech and drops late text for the stale response', () => {
  const model = new TranscriptModel();
  model.receive({ type: 'input_audio_buffer.speech_started' });
  model.receive({ type: 'response.created', response: { id: 'old-response' } });
  model.receive({ type: 'response.output_audio_transcript.delta', response_id: 'old-response', item_id: 'old-item', delta: 'An answer already' });
  model.receive({ type: 'input_audio_buffer.speech_started' });
  model.receive({ type: 'response.output_audio_transcript.delta', response_id: 'old-response', item_id: 'old-item', delta: ' stale ending' });
  assert.equal(model.messages.length, 1);
  assert.equal(model.messages[0].text, 'An answer already');
  assert.equal(model.messages[0].interrupted, true);

  model.receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'input-2', transcript: 'Please switch languages.' });
  model.receive({ type: 'response.created', response: { id: 'new-response' } });
  model.receive({ type: 'response.output_audio_transcript.delta', response_id: 'new-response', item_id: 'new-item', delta: 'Claro.' });
  assert.deepEqual(model.messages.map(({ role, text }) => [role, text]), [
    ['assistant', 'An answer already'],
    ['user', 'Please switch languages.'],
    ['assistant', 'Claro.'],
  ]);
});

test('maps provider states and errors to recoverable UI callbacks', () => {
  const statuses = [];
  const errors = [];
  const model = new TranscriptModel({ onStatus: (value) => statuses.push(value), onError: (value) => errors.push(value) });
  model.receive({ type: 'input_audio_buffer.speech_started' });
  model.receive({ type: 'input_audio_buffer.speech_stopped' });
  model.receive({ type: 'response.created', response: { id: 'r1' } });
  model.receive({ type: 'response.output_audio_transcript.delta', response_id: 'r1', delta: 'Hello' });
  model.receive({ type: 'error', error: { message: 'sensitive server detail' } });
  assert.deepEqual(statuses, ['listening', 'thinking', 'thinking', 'speaking', 'error']);
  assert.match(errors[0].message, /End the conversation/);
  assert.equal(errors[0].message.includes('sensitive server detail'), false);
});

test('shows provider assistant output_text notices as text, not spoken transcript', () => {
  const statuses = [];
  const model = new TranscriptModel({ onStatus: (value) => statuses.push(value) });
  const item = { id: 'notice-1', type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'The personalized service is unavailable. Please try again.' }] };
  model.receive({ type: 'conversation.item.added', item });
  model.receive({ type: 'conversation.item.done', item });
  assert.equal(model.messages.length, 1);
  assert.equal(model.messages[0].text, item.content[0].text);
  assert.equal(model.messages[0].textOnly, true);
  assert.equal(model.messages[0].complete, true);
  assert.deepEqual(statuses, ['listening']);
});
