import test from 'node:test';
import assert from 'node:assert/strict';
import { endCallSafely } from '../end-call.js';

test('failed server deletion keeps call reference and lets user retry End', async () => {
  const call = { attempts: 0, async end() {
    this.attempts += 1;
    if (this.attempts === 1) throw new Error('The server could not confirm that the voice session ended. Keep this page open and try End again.');
  } };
  const state = { activeCall: call, ending: false, statuses: [], errors: [], updates: 0 };
  const options = {
    setEnding: (value) => { state.ending = value; },
    setStatus: (value) => state.statuses.push(value),
    onEnded: () => { state.activeCall = null; },
    onError: (error) => state.errors.push(error),
  };

  assert.equal(await endCallSafely(state.activeCall, options), false);
  assert.equal(state.activeCall, call);
  assert.equal(state.ending, false);
  assert.equal(state.statuses.at(-1), 'error');
  assert.match(state.errors[0].message, /try End again/i);

  assert.equal(await endCallSafely(state.activeCall, options), true);
  assert.equal(state.activeCall, null);
  assert.equal(state.ending, false);
  assert.equal(call.attempts, 2);
});
