import test from 'node:test';
import assert from 'node:assert/strict';
import { getAuthSession, login, logout, register } from '../auth-api.js';

test('session is fetched with same-origin cookie and exposes no IDs', async () => {
  let request;
  const session = await getAuthSession(async (url, options) => {
    request = { url, options };
    return Response.json({ authenticated: true, username: 'Neeraj', user_id: 'must-not-be-returned' });
  });
  assert.deepEqual(session, { authenticated: true, username: 'Neeraj' });
  assert.equal(request.url, '/api/auth/session');
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal('user_id' in session, false);
});

test('login and register send only username and password with same-origin credentials', async () => {
  const requests = [];
  const fetchImpl = async (url, options) => {
    requests.push({ url, options });
    return Response.json({ authenticated: true, username: 'demo' });
  };
  await login('person', 'password', fetchImpl);
  await register('person', 'password', fetchImpl);
  for (const request of requests) {
    assert.equal(request.options.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(request.options.body), { username: 'person', password: 'password' });
    assert.equal('Authorization' in request.options.headers, false);
  }
});

test('logout posts without request body and accepts 204 cookie clearing', async () => {
  let request;
  await logout(async (url, options) => {
    request = { url, options };
    return new Response(null, { status: 204 });
  });
  assert.equal(request.url, '/api/auth/logout');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.credentials, 'same-origin');
  assert.equal('body' in request.options, false);
});

test('maps login conflict and credential errors without echoing server details', async () => {
  for (const [status, phrase] of [[401, /did not match/], [409, /already in use/]]) {
    await assert.rejects(
      login('person', 'password', async () => new Response('private server detail', { status })),
      (error) => phrase.test(error.message) && !error.message.includes('private server detail'),
    );
  }
});
