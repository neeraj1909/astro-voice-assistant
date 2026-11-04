import test from "node:test";
import assert from "node:assert/strict";
import { createChatClient, getSessionIdentity } from "../api.js";

test("chat client posts the expected payload and returns a controlled response", async () => {
  let request;
  const send = createChatClient(async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ response: "Take a breath." }) };
  });
  const result = await send({ user_id: "u1", session_id: "s1", message: "  What now? " });
  assert.equal(request.url, "/api/chat");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(JSON.parse(request.options.body), { user_id: "u1", session_id: "s1", message: "What now?" });
  assert.equal(result.response, "Take a breath.");
});

test("chat client validates input and reports HTTP, network, and malformed response failures", async (t) => {
  const valid = { user_id: "u", session_id: "s", message: "hello" };
  const unused = createChatClient(async () => { throw new Error("should not be called"); });
  await assert.rejects(unused({ ...valid, message: " " }), /required/);
  await t.test("HTTP error", async () => {
    const send = createChatClient(async () => ({ ok: false, status: 503, json: async () => ({ detail: "Unavailable" }) }));
    await assert.rejects(send(valid), /Unavailable/);
  });
  await t.test("network error", async () => {
    const send = createChatClient(async () => { throw new Error("offline"); });
    await assert.rejects(send(valid), /Could not reach/);
  });
  await t.test("malformed response", async () => {
    const send = createChatClient(async () => ({ ok: true, json: async () => ({ nope: true }) }));
    await assert.rejects(send(valid), /unreadable response/);
  });
});

function memoryStorage(values = new Map()) {
  return { values, getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
}

test("user identity persists across browser sessions while session identity rotates", () => {
  const localStorage = memoryStorage();
  let id = 0;
  const randomUUID = () => `id-${++id}`;
  const first = getSessionIdentity({ localStorage, sessionStorage: memoryStorage(), randomUUID });
  const second = getSessionIdentity({ localStorage, sessionStorage: memoryStorage(), randomUUID });
  assert.deepEqual(first, { user_id: "web-id-1", session_id: "id-2" });
  assert.equal(second.user_id, first.user_id);
  assert.notEqual(second.session_id, first.session_id);
});

test("blank and oversized stored identities are regenerated", () => {
  const localStorage = memoryStorage(new Map([["starlight-chat-user-id", " "]]));
  const sessionStorage = memoryStorage(new Map([["starlight-chat-session-id", "x".repeat(129)]]));
  let id = 0;
  const identity = getSessionIdentity({ localStorage, sessionStorage, randomUUID: () => `fresh-${++id}` });
  assert.deepEqual(identity, { user_id: "web-fresh-1", session_id: "fresh-2" });
  assert.equal(localStorage.getItem("starlight-chat-user-id"), identity.user_id);
  assert.equal(sessionStorage.getItem("starlight-chat-session-id"), identity.session_id);
});

test("identity generation continues when browser storage throws", () => {
  const unavailable = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  const identity = getSessionIdentity({ localStorage: unavailable, sessionStorage: unavailable, randomUUID: () => "local-id" });
  assert.deepEqual(identity, { user_id: "web-local-id", session_id: "local-id" });
});
