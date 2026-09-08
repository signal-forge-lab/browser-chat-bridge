# Browser Chat Bridge design

## Contract

One DSH run normally owns one server-side Spark conversation. Turns inside that
run reuse the same conversation; a different run starts a different
conversation. Spark file-tool tasks may instead complete as an unbound
`/spark/tasks` execution. The cloud chat/task UI is the browser-side authority,
so callers send only the new prompt for the current turn.

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

The first completed durable chat turn binds:

```text
run_id -> conversation_id + conversation_url
```

Later durable chat turns must return the exact same binding or fail closed with
`CONVERSATION_MISMATCH`. A completed `/spark/tasks` execution is intentionally
unbound and is cached by `request_id` without creating a run conversation
binding.

Fresh runs open `/spark#bcb-<random>` in a background target. The fragment is not
sent in HTTP requests and exists only to distinguish the newly created target
from stale empty `/spark` tabs during the CDP reconnect.

After the exact first User turn is proven persisted, Spark either promotes the
turn to durable `/spark/chat/<conversation-id>` or moves the execution to
`/spark/tasks`. A Playwright CDP attach does not reliably learn about targets
created after that attach. The Driver therefore observes the loopback CDP
registry and page URL, accepts a correlated Spark task page immediately, or
reattaches Playwright to the single new durable chat target. It does not poll
Gemini backend conversation APIs for discovery.

## Driver interface

```text
POST /v1/turn
{
  "request_id": "...",
  "conversation_url": null | "https://gemini.google.com/spark/chat/<id>",
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

Spark exposes no model picker. The route implicitly uses Gemini 3.8 Flash, so
the Driver performs no model-selection UI interaction. The logical DSH model id
may remain `gemini-3.8-flash-ui`; it denotes the Spark route, not an explicit UI
selection.

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

Spark can delay publication/hydration of a fresh durable chat or task target.
`CHAT_DRIVER_PROMOTION_TIMEOUT_S` defaults to 120 seconds and governs this
structural wait; it is not a fixed sleep.

## Chromium and Obscura

The Driver uses the same Playwright-over-CDP transaction for both. Configure:

```text
CHAT_DRIVER_BACKEND=chromium|obscura
CHAT_DRIVER_CDP_ENDPOINT=http://127.0.0.1:<port>
```

Chromium is the production baseline because the Gemini contract was live-probed
there. Obscura is implemented at the same seam but remains shadow/experimental
until authenticated Spark hydration, send confirmation, and
response completion all pass repeatedly. No Google authentication cookies are
copied into Obscura automatically.

