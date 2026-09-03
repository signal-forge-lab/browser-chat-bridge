# Browser Chat Bridge design

## Contract

One DSH run owns one server-side Gemini conversation. Turns inside that run reuse
the same conversation; a different run starts a different conversation. The
cloud chat is the conversation-history authority, so callers send only the new
prompt for the current turn.

```text
DSH / caller
  -> Bridge process : run lifecycle, idempotency, run -> conversation binding
  -> Driver process : browser transaction only
  -> CDP browser     : Chromium today; Obscura through the same Driver contract
  -> Gemini UI       : server-side conversation history
```

The processes are deliberately separate. A CDP/browser failure must not destroy
the caller-facing run/idempotency state, and the Driver must not know DSH run
semantics.

## Bridge interface

```text
POST /v1/runs/{run_id}/turn
{"request_id":"...", "prompt":"..."}
```

`request_id` is globally idempotent. The Bridge persists `DISPATCHING` before
calling the Driver. If the Driver connection disappears after that point, the
result is `AMBIGUOUS` and the same request is never blindly resent.

The first completed turn binds:

```text
run_id -> conversation_id + conversation_url
```

Later turns must return the exact same binding or fail closed with
`CONVERSATION_MISMATCH`.

Fresh runs open `/app#bcb-<random>` in a background target. The fragment is not
sent in HTTP requests and exists only to distinguish the newly created target
from stale empty `/app` tabs during the CDP reconnect.

After the exact first User turn is proven persisted, Gemini may create the
durable `/app/<conversation-id>` target after the current Playwright CDP attach.
That attach does not reliably learn about later targets. The Driver therefore
observes only the loopback CDP `/json/list` registry, waits for exactly one new
durable Gemini URL relative to the pre-send baseline, and then reattaches
Playwright once to hydrate that exact target. It does not poll Gemini backend
conversation APIs for discovery.

## Driver interface

```text
POST /v1/turn
{
  "request_id": "...",
  "conversation_url": null | "https://gemini.google.com/app/<id>",
  "prompt": "..."
}
```

The Driver owns no run database. `conversation_url=null` means create a fresh
Gemini chat; otherwise it reuses/reopens that exact durable conversation.

## Concurrency

Bridge requests are serialized per `run_id`, because a chat thread is ordered.
Different runs can execute in parallel. Driver requests use separate CDP client
connections, so no run-global browser mutex is required.

## Fixed model

Every turn verifies the Gemini picker before touching the composer. The required
state is Gemini 3.8 Flash with `強化版思考モード`; the current UI renders the
selected pill as `Flash / 拡張`. If the fixed state cannot be proven or repaired,
the Driver returns `MODEL_MISMATCH` and sends nothing.

## Dispatch safety

The Driver snapshots User/Assistant counts, fills the Quill contenteditable via
Playwright, reads the composer back exactly, then clicks the unique send button.
After click returns, only a new `user-query` with exact prompt equality confirms
dispatch. Failure to prove that turn is `AMBIGUOUS`, never `NOT_DISPATCHED`.

This distinction is load-bearing. During the 2026-09-04 live probe, a synthetic
CDP click looked uncommitted, then persisted late. A second attempt produced two
identical User turns. The production path therefore forbids blind resend after
any possibly side-effecting submit.

## Completion

The response is the single next `model-response`. Completion requires all of:

- exactly one new model response;
- `message-content .markdown[aria-busy="false"]`;
- `.response-footer.complete` inside that same response;
- non-empty response text stable for two samples.

The returned text comes only from that anchored model response.

Enhanced mode can delay publication/hydration of the fresh durable target for
around a minute. `CHAT_DRIVER_PROMOTION_TIMEOUT_S` defaults to 120 seconds and
governs this structural wait; it is not a fixed sleep.

## Chromium and Obscura

The Driver uses the same Playwright-over-CDP transaction for both. Configure:

```text
CHAT_DRIVER_BACKEND=chromium|obscura
CHAT_DRIVER_CDP_ENDPOINT=http://127.0.0.1:<port>
```

Chromium is the production baseline because the Gemini contract was live-probed
there. Obscura is implemented at the same seam but remains shadow/experimental
until authenticated Gemini hydration, model selection, send confirmation, and
response completion all pass repeatedly. No Google authentication cookies are
copied into Obscura automatically.

