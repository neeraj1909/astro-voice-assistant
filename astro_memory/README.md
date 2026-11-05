# MyNaksh integration

The Python modules in this directory were copied from the user's local
`~/Code/mynaksh_assignment/app` project on 2026-11-05. No license file was found
in that local source. The source repository remains unchanged. `storage.py`
keeps the existing SQLite schema, so the configured MyNaksh database can be
used without migration.

Start `uvicorn astro_memory.main:app` with `LLM_PROVIDER=openai-compatible`,
`LLM_BASE_URL` (for OpenAI, `https://api.openai.com/v1`), `LLM_API_KEY`,
`LLM_MODEL`, and `DATABASE_PATH` set in the server process. The gateway holds
credentials server-side. The normal mode retains the original sequential
context and reply agents. `ASTRO_MEMORY_FAST=1` selects the experimental
single-call variant, which passes the authenticated user's bounded candidate
memory values to the model before relevance selection. This can change
relevance and allow irrelevant context to affect the answer; it must be
validated with real model and multilingual memory journeys before production.
Unquoted current-message evidence is required before saving memory or language
preferences. This is a general personal conversation demo; model instructions
are not a hard security boundary. The upstream app still accepts caller-provided user
IDs, so keep it on loopback behind the Astro gateway.

When enabled, the fast service keeps bounded per-user memory and per-session
history in RAM. The authenticated gateway can preload them at sign-in via a
loopback-only, shared-secret internal endpoint; the first turn also loads them
on demand if preloading fails. Committed writes are reflected in the cache.
The service must be the sole writer to its SQLite database while this cache is
in use; external writers can make cached data stale. Eviction reloads from
SQLite, and process restart discards the cache without deleting stored data.
Prompt limits can still omit older history or candidate memory values.

Turns for the same user are serialized across the model call to keep memory and
history updates consistent. A new turn may therefore wait for an older model
call unless that call is canceled. Canceling releases the lock, but a user
message already committed before cancellation remains in session history.
