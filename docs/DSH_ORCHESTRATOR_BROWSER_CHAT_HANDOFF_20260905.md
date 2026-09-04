# DSH Orchestrator × Browser Chat handoff — 2026-09-05

## Purpose

This document is the handoff for adding the already-working `browser-chat`
subagent provider to the DSH orchestrator in another development chat.

It covers two separate states and must not blur them:

1. **Already implemented and live-proven** — DSH can call Browser Chat through
   `subagentType: browser-chat`; the browser-side model is fixed by the Driver.
2. **Requested next change, not yet implemented at this handoff** — allow at
   most two concurrent Browser Chat executions and make the third and later
   requests fail immediately in a way that makes the DSH orchestrator fall
   back to another candidate.

Do not report the concurrency cap as implemented until the acceptance tests in
this document pass in the current checkout and deployed profile.

---

## Canonical repositories and current commits

### Browser Chat Bridge

Repository:

```text
C:\Users\shogo\Documents\Intelligence Works\github\browser-chat-bridge
```

Current reviewed commit at handoff:

```text
391cd42 Harden browser chat cleanup and CDP lifecycle
```

The working tree was clean when this handoff was written.

Important files:

```text
README.md
src/browser_chat_bridge/bridge.py
src/browser_chat_bridge/bridge_server.py
src/browser_chat_bridge/driver_server.py
src/browser_chat_bridge/gemini.py
src/browser_chat_bridge/store.py
scripts/start.ps1
scripts/status.ps1
scripts/stop.ps1
docs/DSH_ORCHESTRATOR_HANDOFF_20260904.md
docs/DSH_BROWSER_CHAT_PROVIDER_PLAN_20260904.md
```

### DeepSeek Harness Browser Chat provider worktree

Repository family:

```text
C:\Users\shogo\Documents\Intelligence Works\github\deepseek-harness
```

Current Browser Chat provider branch/worktree commit at handoff:

```text
4aa923142b fix(subagent): harden browser chat lifecycle
```

Important package:

```text
packages/subagent/subagent-browser-chat/
```

Important implementation:

```text
packages/subagent/subagent-browser-chat/src/run.ts
```

The provider worktree was clean when this handoff was written.

### Deployed DSH profile

The `web` profile has the Browser Chat package installed from the reviewed rc.2
compatibility tarball:

```text
@deepseek-ai/dsh-subagent-browser-chat
file:~/.dsh/packages/deepseek-ai-dsh-subagent-browser-chat-0.1.1-rc.2-compat2.tgz
```

The temporary `headless` profile installation used for a smoke test was removed
after verification; do not assume it should remain installed there.

---

## Runtime topology

The production path is:

```text
DSH Orchestrator / External Workflow
  -> SubagentProvider: browser-chat
  -> Browser Chat Bridge  http://127.0.0.1:8765
  -> Browser Chat Driver  http://127.0.0.1:8766
  -> AegisChrome CDP      dynamic local port
  -> https://gemini.google.com/
```

The Browser Chat launcher/status scripts detect the current AegisChrome CDP
port. At the final verification of the previous task it was `9339`, but the port
is operational state, not a configuration constant. Always inspect current
health instead of hard-coding that number.

Current health commands live in:

```text
scripts/start.ps1
scripts/status.ps1
scripts/stop.ps1
```

The scripts only manage Bridge/Driver processes that they recorded themselves;
they must not kill arbitrary Edge/Chrome processes.

---

## How the orchestrator must select Browser Chat

### Correct selection

Browser Chat is a DSH `SubagentProvider`, not an LLM model tier.

Select it with:

```text
subagentType: browser-chat
```

Conceptual workflow example:

```js
await wf.runAgent({
  name: 'browser-review',
  prompt: 'Review the supplied material and return the requested result.',
  subagentType: 'browser-chat',
  readOnly: true,
  sensitivity: 'public',
})
```

Use the exact API shape of the current External Workflow/Orchestrator source;
the invariant is the `subagentType: browser-chat` transport selection.

### Do NOT configure these for Browser Chat

Do not put Browser Chat into a normal LLM route by setting any of these:

```text
model: browser-chat
provider: browser-chat
model: Gemini 3.8 Flash
provider/model overrides in agentOptions
modelHint as a substitute for subagentType
```

The Browser Chat provider intentionally rejects injected LLM
`agentOptions.provider/model` values.

### Browser-side model ownership

The Driver remains authoritative for the UI model:

```text
Gemini 3.8 Flash
+ 強化版思考モード
```

The orchestrator must not add a model or reasoning-effort field that can
silently override that browser selection.

---

## Proven Browser Chat lifecycle

Identity is intentionally one-to-one:

```text
one DSH SubagentProvider.start()
  <-> one DSH Browser Chat run id
  <-> one Bridge run_id
  <-> one Gemini conversation
```

Normal run:

```text
turn:1
  -> Gemini result
  -> completed
  -> dispose()
  -> DELETE /v1/runs/{run_id}
  -> Gemini conversation deleted
  -> local Bridge run/turn cache purged
```

Structured output may use exactly one repair turn:

```text
turn:1 invalid JSON/schema
  -> turn:2 on SAME run / SAME conversation
  -> valid result or fail closed
```

No third repair turn exists.

Cancellation is conservative:

- local result can become `aborted` immediately;
- an already-dispatched Bridge POST is neither blindly resent nor force-cancelled;
- `dispose()` waits for that one POST to reach a terminal Bridge result;
- cleanup then removes the ephemeral Gemini conversation.

`AMBIGUOUS` never authorizes a blind resend.

---

## Already-proven acceptance state

The previous implementation/review completed with the following evidence:

```text
Browser Chat Bridge unit              20/20 PASS
DSH Browser Chat provider unit        39/39 PASS
REAL Loader composition                1/1 PASS
DSH Browser Chat TypeScript build         PASS
DSH host build                            PASS
real Gemini LIVE E2E                  4/4 PASS
External Workflow -> Browser Chat -> Gemini PASS
Bridge DB after cleanup               runs=0 / turns=0
known Browser Chat test titles        MATCHES 0
```

The External Workflow smoke proved a real path through:

```text
workflow completed
  -> one agent spawned
  -> browser-chat provider
  -> Bridge
  -> Driver
  -> real Gemini reply
  -> workflow run-end
  -> process exit 0
  -> conversation cleanup
```

These are regression gates for the concurrency change.

---

## New requirement: maximum concurrent Browser Chat executions = 2

### User-visible contract

The requested behavior is:

```text
Browser Chat execution #1 -> admitted
Browser Chat execution #2 -> admitted
Browser Chat execution #3 -> reject immediately, before browser side effect
                           -> tell caller capacity is unavailable
                           -> DSH orchestrator falls back to next candidate
```

The same applies to #4, #5, and later requests while both slots are occupied.

No wait queue is desired for overflow requests. The purpose of the cap is to
let the orchestrator use another provider/model instead of piling work onto
Browser Chat.

After one slot becomes free, a later Browser Chat request must be admitted
normally again.

### Scope of the limit

The authoritative limit should live at the shared Bridge boundary so multiple
DSH callers/profiles cannot each believe they have their own two slots.

Recommended first implementation:

- process-wide non-blocking capacity gate in `BridgeService`;
- default capacity `2`;
- optional environment/config override is acceptable, but default must remain
  `2` for this deployment;
- acquire before a new side-effecting Driver dispatch;
- if no permit is available, return typed `BUSY` immediately;
- release the permit in `finally` when that `run_turn` invocation finishes;
- do not create a wait queue.

This limits concurrent admitted/in-flight Bridge turn executions. A structured
repair turn reacquires a slot like any other turn.

### Important existing serialization

`BridgeService` already contains `_new_conversation_lock` because Gemini target
promotion for a new `/app` conversation is difficult to correlate safely.

At handoff, unbound first turns are intentionally serialized around the Driver
call. Therefore adding a maximum of two does **not** imply that this task should
remove `_new_conversation_lock` or make two brand-new Gemini conversation
creations truly parallel.

Do not remove or weaken that lock merely to increase throughput. The requested
change is an **upper bound and fallback contract**, not a throughput refactor.
Any change to first-turn correlation concurrency requires a separate review and
live proof.

---

## Capacity rejection must be pre-dispatch and retry-safe

The overflow request must be rejected before any browser side effect.

Recommended typed Bridge reply:

```json
{
  "request_id": "...",
  "run_id": "...",
  "status": "BUSY",
  "content": null,
  "conversation_id": null,
  "conversation_url": null,
  "error": "browser-chat capacity unavailable; maximum concurrent executions is 2",
  "cached": false
}
```

The exact wording can differ, but it must remain fixed/content-free and contain
a stable capacity signal.

Prefer not to persist a pre-dispatch `BUSY` as the terminal idempotency result
for that `request_id`. Capacity rejection has not crossed the side-effecting
boundary, so the same logical request may be safely attempted later if a caller
explicitly chooses to do so. The DSH orchestrator itself should fall back rather
than auto-looping on Browser Chat.

Preserve request-id conflict protection for already-existing rows.

---

## Provider mapping for BUSY

The Browser Chat provider already recognizes `BUSY` as a valid Bridge status.

For capacity BUSY, return an error result with a fixed diagnostic that exposes
capacity semantics without prompt/reply content, for example:

```text
browser-chat capacity unavailable (stage: turn; status: BUSY; not dispatched)
```

Required properties:

- `stopReason: 'error'`;
- no output content;
- no blind resend;
- no prompt/reply/schema content in the diagnostic;
- `dispose()` remains safe/idempotent;
- because no Bridge run was created, cleanup may legitimately return
  `NOT_FOUND`.

---

## DSH orchestrator fallback semantics

### Current dispatcher behavior that matters

The deployed `iw-dsh-workflow-dispatch` currently classifies messages matching
`capacity`, `overload`, `temporar...`, transport failures, selected HTTP 4xx/5xx,
etc. as `transient_provider_error`.

That is enough to make the dispatcher choose a fallback candidate, but it also
feeds the failure into `ModelPolicyResolver.recordFailure()`.

`recordFailure()` opens a candidate circuit, and `transient_provider_error`
also opens a provider-level circuit. Therefore simply making the diagnostic
contain `capacity` can over-penalize Browser Chat after a momentary two-slot
capacity event.

### Required behavior for this integration

A capacity overflow is not evidence that Browser Chat is unhealthy.

The DSH side should distinguish capacity saturation from an actual transient
provider outage. The desired semantics are:

```text
capacity full
  -> immediate fallback for THIS task
  -> advance to next candidate for THIS phase/task
  -> do NOT open a long-lived provider-unhealthy circuit
  -> do NOT wait/retry Browser Chat before falling back
  -> later tasks may select Browser Chat again as soon as capacity is free
```

Recommended classification name:

```text
capacity_busy
```

or another clearly equivalent typed classification.

Implementation detail is left to the DSH development chat, but the acceptance
behavior above is mandatory. It is acceptable to implement a dedicated
capacity branch instead of widening the generic provider-failure regex.

Do not treat capacity overflow as:

- a quality failure;
- a rate limit requiring a retry delay;
- an authentication/model-unavailable failure;
- a reason to disable the Browser Chat provider for the full circuit TTL.

---

## Orchestrator configuration model

Browser Chat should be represented as a transport/subagent choice, not as a
model string.

Conceptual policy:

```yaml
candidateChain:
  - transport: browser-chat
    subagentType: browser-chat
  - transport: dsh-model
    modelHint: balanced
  - transport: dsh-model
    modelHint: deep
```

If the current orchestrator internally represents candidates differently,
preserve its central routing authority. Do not add arbitrary provider/model
escape fields to workflow source just for Browser Chat.

The important runtime result is:

```text
browser-chat has slot
  -> use browser-chat

browser-chat BUSY/capacity
  -> next configured candidate
```

---

## Required tests for the concurrency change

### 1. Bridge capacity unit test

Use a fake Driver barrier so two calls remain in flight.

Prove:

1. call A enters Driver;
2. call B is admitted as the second capacity user;
3. call C returns `BUSY` immediately without entering Driver;
4. Driver call count remains two;
5. call C creates no browser-side effect;
6. after one permit is released, a later call can enter normally;
7. no semaphore/permit leak on exception paths.

Because `_new_conversation_lock` can serialize unbound Driver calls, the test
may need one or more bound-run fixtures to prove two permits independently of
the new-conversation correlation lock. Do not weaken that lock just to make the
test easier.

### 2. Browser Chat provider unit test

Feed a Bridge `BUSY` capacity reply and prove:

```text
stopReason = error
diagnostic contains stable capacity signal
no output
one Bridge attempt only
no repair turn
dispose idempotent
no content leakage
```

### 3. DSH dispatch fallback test

Prove a Browser Chat capacity result causes the next configured candidate to
start and complete.

Also prove:

- fallback telemetry records a capacity-specific reason;
- no Browser Chat resend occurs;
- no long-lived Browser Chat provider circuit is opened by capacity alone;
- a later phase/task can use Browser Chat immediately after capacity is free.

### 4. Three-request integration test

Run three orchestrator tasks concurrently with Browser Chat first in their
candidate chain.

Acceptance:

```text
at most two Browser Chat capacity slots admitted
third task falls back
third task creates no Gemini Browser Chat turn
first/second runs clean their Gemini conversations
after capacity clears, a new Browser Chat task is admitted
```

If current `_new_conversation_lock` means only one new Gemini conversation is
actually inside Driver at once, that is acceptable for this task. The test is
for admission/fallback correctness, not for removing the correlation lock.

### 5. Regression gates

Re-run at minimum:

```text
Browser Chat Bridge unit suite
DSH Browser Chat provider focused suite
REAL Loader composition
DSH Browser Chat TypeScript project build
DSH host build or the current equivalent focused host gate
real Gemini Browser Chat smoke
External Workflow -> browser-chat positive smoke
```

Do not finish with only mocks if live Browser Chat is available.

---

## Security and correctness invariants that must remain unchanged

Do not regress any of these:

- `bridgeBaseUrl` remains loopback-only;
- Browser Chat POST redirects remain rejected (`redirect: 'error'`);
- `AMBIGUOUS` is never blindly resent;
- one provider run maps to one Bridge run and one Gemini conversation;
- structured repair stays on the same run/conversation and is limited to one;
- diagnostics remain content-free;
- workflow/model source cannot override the Driver-fixed Gemini model;
- DSH Browser Chat runs clean their generated Gemini conversations on dispose;
- user-owned Gemini conversations must never be bulk-deleted by guesswork;
- launcher scripts do not kill arbitrary Edge/Chrome processes;
- temporary test profile/package changes are removed after smoke verification.

---

## Definition of done

Treat the concurrency/fallback work as 100% complete only when all of the
following are true:

1. Browser Chat selection works through `subagentType: browser-chat` with no
   model/provider override.
2. Shared Browser Chat capacity defaults to 2.
3. A third concurrent request is rejected before browser dispatch.
4. Overflow is returned as typed capacity/BUSY state with no prompt leakage.
5. DSH immediately falls back to the next candidate.
6. Capacity saturation does not poison Browser Chat's provider circuit after
   capacity becomes free.
7. No overflow wait queue exists.
8. Existing `AMBIGUOUS` zero-resend semantics remain intact.
9. Structured repair and cleanup still work.
10. Unit, composition, host/build, live Browser Chat, and External Workflow
    regression gates pass.
11. Any generated test Gemini conversations are deleted.
12. Deployment/profile is updated and current runtime health is confirmed.
13. Final independent code review has no blocking findings.
14. Changes are committed and relevant working trees are clean.

---

## What the next development chat should inspect first

Do not implement only from this handoff. Re-read current files/Git because a
newer change always overrides this cached state.

Start with:

```text
browser-chat-bridge:
  git status / git log
  src/browser_chat_bridge/bridge.py
  src/browser_chat_bridge/store.py
  tests/test_bridge_service.py

deepseek-harness Browser Chat worktree:
  git status / git log
  packages/subagent/subagent-browser-chat/src/run.ts
  packages/subagent/subagent-browser-chat/tests/subagent-browser-chat.spec.ts

DSH orchestrator deployment/source:
  current WorkflowDispatch implementation
  failureEvidence / failure classification
  ModelPolicyResolver.recordFailure
  current candidate/fallback policy
```

The critical DSH review question is not merely "does BUSY fall back?" It is:

> Does a Browser Chat capacity-only BUSY fall back for the current task without
> incorrectly opening a provider-unhealthy circuit that suppresses Browser Chat
> after a slot is free?

