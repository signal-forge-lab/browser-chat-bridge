# DSH Orchestrator Browser Chat integration prompt — 2026-09-05

Use the following prompt in the DSH development chat together with:

```text
docs/DSH_ORCHESTRATOR_BROWSER_CHAT_HANDOFF_20260905.md
```

---

## Prompt

Treat the attached handoff as the previous chat's implementation context, but
prefer current files, Git state, deployed profile state, and live process state
over any cached statement in the document.

The Browser Chat provider itself is already implemented and live-proven. The
current intended DSH usage is:

```text
subagentType: browser-chat
```

Browser Chat is NOT a normal DSH model/provider route. Do not configure
`model: browser-chat`, do not inject `provider/model`, and do not add a workflow
escape hatch that can override the Driver-fixed Gemini model. The Driver owns:

```text
Gemini 3.8 Flash + 強化版思考モード
```

The main new requirement is:

```text
maximum concurrent Browser Chat executions = 2

1st -> Browser Chat
2nd -> Browser Chat
3rd and later while both slots are occupied
    -> reject immediately before browser side effect
    -> no wait queue
    -> return a stable typed capacity/BUSY failure
    -> DSH orchestrator falls back to the next configured candidate
```

Implement this end-to-end, including the DSH fallback semantics.

Important design constraints:

1. Put the authoritative capacity gate at the shared Browser Chat Bridge
   boundary so multiple callers cannot each get an independent limit.
2. Use non-blocking admission. Do not queue overflow calls.
3. Capacity rejection must happen before a side-effecting Driver send.
4. Preserve request-id/idempotency conflict safety.
5. Preserve `_new_conversation_lock`; do NOT remove it to chase throughput.
   The requested work is an upper-bound/fallback feature, not a redesign of
   Gemini first-turn target correlation.
6. Provider diagnostics for capacity must be fixed/content-free and must not
   echo prompts, replies, schema data, URLs, cookies, or raw payloads.
7. `AMBIGUOUS` remains zero-resend.
8. Structured repair remains same-run/same-conversation and max one repair.
9. DSH Browser Chat dispose/cleanup must keep deleting automated Gemini
   conversations.

Before changing DSH fallback logic, inspect the current deployment/source.
Current observed behavior at handoff:

- `failureEvidence()` treats `capacity`/`overload`-like messages as
  `transient_provider_error`;
- normal `recordFailure()` opens candidate circuits;
- `transient_provider_error` also opens a provider-level circuit.

That default is too broad for a simple two-slot saturation event. A Browser
Chat capacity BUSY should fall back for the current task without making the
provider appear unhealthy after capacity clears.

Implement a capacity-specific semantic, for example `capacity_busy`, or an
equivalent dedicated branch. Required behavior:

```text
capacity full
  -> immediate current-task fallback
  -> no Browser Chat resend
  -> no retry delay / wait-for-capacity queue
  -> no long-lived provider-unhealthy circuit from capacity alone
  -> later task may use Browser Chat again once a slot is free
```

Do not merely add the word `capacity` to a diagnostic and stop there; verify
the resulting circuit behavior.

Implement tests first or in RED/GREEN order. At minimum prove:

1. Bridge: two capacity users admitted, third gets immediate BUSY and never
   reaches the fake Driver; after a slot frees a later request is admitted.
2. Provider: BUSY capacity maps to one fixed error result with no resend,
   no repair turn, no leaked content, and idempotent dispose.
3. Dispatcher: capacity BUSY falls back to the next candidate but does not
   poison the Browser Chat provider circuit.
4. Concurrent integration: three orchestrator tasks produce at most two
   Browser Chat admissions and the overflow task uses the fallback candidate.
5. Regression: Browser Chat unit, DSH provider unit, REAL Loader composition,
   TypeScript/host build, live Browser Chat smoke, and External Workflow ->
   browser-chat smoke remain green.

Current reviewed baselines from the handoff are:

```text
browser-chat-bridge commit: 391cd42
DSH Browser Chat provider commit: 4aa923142b

Browser Chat Bridge unit:       20/20 PASS
DSH provider unit:              39/39 PASS
REAL Loader composition:         1/1 PASS
real Gemini LIVE E2E:            4/4 PASS
External Workflow integration:      PASS
```

Re-read current files and do not assume those hashes are still HEAD.

Finish the task end-to-end: implementation, tests, live verification when the
browser environment is available, deployment/profile update if needed, final
independent review, repairs, re-review, commit, and clean working-tree check.

Use 100% to mean all requested implementation/review/deployment verification is
complete. Do not call the task 100% while a required live or fallback acceptance
gate is still unproven.

Do not start unrelated DSH routing work.

