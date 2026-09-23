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

Fresh runs first reuse one existing, structurally empty `/spark` tab inside the
dedicated BrowserChatEdge profile when exactly one such tab is available. The
fresh-turn lock makes that reuse single-owner, and the Driver follows that
page's exact CDP `targetId` through promotion. If zero or multiple safe
candidates exist, the Driver opens `/spark#bcb-<random>` as a new active tab.
The fragment is not sent in HTTP requests and exists only to distinguish a
newly created target during CDP reconnect. Reused pre-existing tabs are never
tagged as automation-owned, entered into the owned-target release registry, or
closed by normal cleanup.

After the exact first User turn is proven persisted, Spark either promotes the
turn to durable `/spark/chat/<conversation-id>` or moves the execution to
`/spark/tasks`. A nodriver CDP attach does not reliably learn about targets
created after that attach. The Driver therefore observes the exact target in the
loopback CDP registry and its page URL, accepts the same target when it becomes a
Spark task page, or reattaches nodriver to its promoted durable chat URL. It
does not poll Gemini backend conversation APIs for discovery.

Current Spark no longer reliably marks the open Task card with
`aria-selected=true`. The live UI instead gives the current Task card
`tabindex=0` while historical cards use `-1`. Task correlation therefore
accepts either signal, but only for a goal id absent from the pre-dispatch
baseline; historical hydration alone can never select a different Task.

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
connections. Bound durable conversations can use the global Bridge capacity in
parallel, but unbound first turns are deliberately serialized twice: the Bridge
`_new_conversation_lock` protects run-to-conversation ownership and the Driver
`_unbound_first_turn_lock` protects Spark target/task correlation. Fresh-run
correlation still prefers the exact automation-created target id. The global
capacity limit applies around these guards; it does not imply that four fresh
Gemini conversations should be created concurrently.

## Fixed model

Spark exposes no model picker. The route implicitly uses Gemini 3.8 Flash, so
the Driver performs no model-selection UI interaction. The logical DSH model id
may remain `gemini-3.8-flash-ui`; it denotes the Spark route, not an explicit UI
selection.

## Dispatch safety

The Driver snapshots User/Assistant counts, fills the Quill contenteditable via
nodriver/CDP Input, reads the composer back exactly, proves the unique Send
button is ready, then submits from the focused composer with a trusted Enter
key. This mirrors the successful manual Browser Chat path while avoiding
Spark's button-triggered Task promotion failure mode observed in live canaries.
Browser/DOM/target ownership remains nodriver-native. After resolving the exact
nodriver-owned target, trusted text and mouse input are dispatched through that
target's loopback CDP WebSocket. The mouse sequence is move, press with
`buttons=1`, release with `buttons=0`; it deliberately does not use a
synthetic DOM `element.click()`. This also avoids the observed live stall in
nodriver's attached-tab `Input.dispatchMouseEvent` request path while preserving
the exact target identity. This is an internal local-CDP path: the Bridge does
not call Lomway, Browser Harness, or any MCP tool.
The current Edge build can delay Input-domain acknowledgement when a target is
created as a background tab. BrowserChatEdge is a dedicated automation profile,
so automation targets are created active (`background=false`) instead. Element
lookup keeps its ordinary five-second budget while the trusted input sequence
also retains a separate ten-second minimum transport budget for attach/runtime
variance.
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

Gemini connector/tool approval UI is not a valid model completion even when it
is rendered inside the response container and the response footer reports
complete. The Driver rejects the known approval-card shape (a request to let
Gemini use a tool/connector, a `Tool:` line, and explicit approve/deny choices)
as `AMBIGUOUS` instead of returning that UI text as `COMPLETED`. This prevents
permission surfaces from being fed into planner/reviewer protocol repair as if
they were model-authored structured output.

Gemini may also render a Drive/file action card inside the same Markdown
container after otherwise valid model text. The Driver strips the rendered
`.attachment-container.action-card` subtree before returning response text, so
file title/date/open UI does not contaminate structured Planner/Reviewer/Worker
protocol output.

## Post-dispatch recovery

The Bridge never blind-resends a turn after a possibly side-effecting click. If
the Driver returns `TIMEOUT`, or the Driver HTTP response is lost after
dispatch, the Bridge may perform one read-only recovery attempt only when a
durable conversation URL is already known. Recovery reattaches to the existing
Gemini target, verifies that the latest persisted User turn exactly matches the
original prompt, and returns the already-existing structurally complete model
response if one is present. It does not click Send, create a new conversation,
or navigate a replacement target. If attribution or completion cannot be
proven, the original fail-closed result remains `TIMEOUT` or `AMBIGUOUS`.

Spark can delay publication/hydration of a fresh durable chat or task target.
`CHAT_DRIVER_PROMOTION_TIMEOUT_S` defaults to 120 seconds and caps only the
promotion phase; it is not a fixed sleep. The Driver also has one turn-wide
response deadline starting before browser attachment. Browser setup, dispatch,
promotion, reattach, and response observation therefore cannot stack independent
budgets beyond the Bridge request bound.

An unbound `/spark/tasks` execution that is still active at the response
deadline is stopped only through the automation-owned page's visible stop
control. If stop is confirmed, the Driver marks the timeout as remotely stopped,
closes that owned page, and the Bridge may purge the local run without a remote
delete. If stop cannot be confirmed, the run remains unresolved for
reconciliation. Durable chat timeouts are not stopped automatically.

## Browser target lifecycle

Cloud conversation lifetime and browser-tab lifetime are separate. A completed
role keeps its durable Gemini conversation and Bridge run history, but releases
the browser target created by the automation. The Driver records the exact CDP
target id and also tags automation-created pages through `window.name`; the tag
lets a restarted Driver recognize orphaned automation tabs without treating a
Human-created Gemini tab as owned.

Tool loops retain the same target while tool calls/results are still in flight.
Direct roles release after a successful response, and EXECUTE roles release
only after the protocol returns `type=final`. `TIMEOUT` and `AMBIGUOUS` are not
released immediately because post-dispatch recovery may still need the live
target. Their durable URLs remain janitor-protected for 30 minutes; after that
window an otherwise idle tagged target is eligible for orphan cleanup. Tagged
automation pages that have not yet acquired a durable chat URL (for example an
unresolved Task timeout) also receive a 30-minute page-age grace before the
janitor may close them.

After a terminal role, the client also requests an orphan sweep. The Bridge
passes the durable URLs of currently active runs as a protection set; the
Driver closes only tagged automation-owned durable-chat tabs outside that set.
Conversation deletion remains the separate `DELETE /v1/runs/<run_id>` path and
is never implied by tab release.

## Chromium and Obscura

The Driver uses the same nodriver-over-CDP transaction for both. Driver health
reports the browser kind separately as `backend` (for example `chromium`)
and the automation client as `automation_backend: nodriver`. The Bridge does
not call the Stealth Browser MCP; MCP remains an external control surface only.
Configure:

```text
CHAT_DRIVER_BACKEND=chromium|obscura
CHAT_DRIVER_CDP_ENDPOINT=http://127.0.0.1:<port>
```

Chromium is the production baseline because the Gemini contract was live-probed
there. Obscura is implemented at the same seam but remains shadow/experimental
until authenticated Spark hydration, send confirmation, and
response completion all pass repeatedly. No Google authentication cookies are
copied into Obscura automatically.

