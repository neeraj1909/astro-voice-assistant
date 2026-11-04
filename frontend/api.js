const validId = (value) => typeof value === "string" && value.trim().length > 0 && value.length <= 128;

function browserStorage(name) {
  try { return globalThis[name]; } catch { return null; }
}

export function getSessionIdentity(options = {}) {
  const localStorage = options.localStorage ?? browserStorage("localStorage");
  const sessionStorage = options.sessionStorage ?? browserStorage("sessionStorage");
  const randomUUID = options.randomUUID ?? (() => crypto.randomUUID());
  const userKey = "starlight-chat-user-id";
  const sessionKey = "starlight-chat-session-id";
  let userId;
  let sessionId;
  try { userId = localStorage?.getItem(userKey); } catch { userId = null; }
  try { sessionId = sessionStorage?.getItem(sessionKey); } catch { sessionId = null; }
  if (!validId(userId)) {
    userId = `web-${randomUUID()}`;
    try { localStorage?.setItem(userKey, userId); } catch { /* Continue with an in-memory ID if storage is unavailable. */ }
  }
  if (!validId(sessionId)) {
    sessionId = randomUUID();
    try { sessionStorage?.setItem(sessionKey, sessionId); } catch { /* Continue with an in-memory ID if storage is unavailable. */ }
  }
  return { user_id: userId.trim(), session_id: sessionId.trim() };
}

export function createChatClient(fetchImpl = globalThis.fetch) {
  return async function sendMessage({ user_id, session_id, message }) {
    if (![user_id, session_id, message].every((value) => typeof value === "string" && value.trim())) {
      throw new TypeError("A user, session, and message are required.");
    }
    let response;
    try {
      response = await fetchImpl("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ user_id, session_id, message: message.trim() }),
      });
    } catch {
      throw new Error("Could not reach the chat service. Check your connection and try again.");
    }
    let data;
    try { data = await response.json(); } catch { data = null; }
    if (!response.ok) {
      throw new Error(data?.detail || data?.error || `The chat service returned an error (${response.status}).`);
    }
    if (!data || typeof data.response !== "string" || !data.response.trim()) {
      throw new Error("The chat service returned an unreadable response. Please try again.");
    }
    return data;
  };
}
