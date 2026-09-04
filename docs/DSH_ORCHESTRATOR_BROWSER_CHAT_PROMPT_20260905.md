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

The Browser Chat provider, Bridge capacity gate, Stable Routing integration,
and Web/Headless profile deployment are already implemented. Direct task
selection remains:

```text
subagentType: browser-chat
```

Browser Chat is not an LLM provider/model exposed to workflow source. Stable
Routing may place it between ordinary model candidates using the central
candidate field `subagentProvider: browser-chat`; the dispatcher must then omit
LLM `agentOptions.provider/model` injection. Do not add a workflow escape hatch
that can override the Driver-fixed Gemini model. The Driver owns:

```text
Gemini 3.8 Flash + 強化版思考モード
```

The shipped capacity contract is:

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

Verify this end-to-end from the current files before making any further change.

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

The current dispatcher uses the dedicated `capacity_busy` semantic. Required
behavior remains:

```text
capacity full
  -> immediate current-task fallback
  -> no Browser Chat resend
  -> no retry delay / wait-for-capacity queue
  -> no long-lived provider-unhealthy circuit from capacity alone
  -> later task may use Browser Chat again once a slot is free
```

Do not regress this into a generic provider-unhealthy circuit.

Current focused baselines are:

```text
Browser Chat Bridge unit:       23/23 PASS
DSH provider unit:              45/45 PASS
Stable Routing dispatch:        58/58 PASS
Web profile bundle:                  PASS
Headless profile bundle:             PASS
Bridge / Driver / CDP health:        PASS
```

The 2026-09-05 live three-request capacity probe proved two admissions and an
immediate third-request `BUSY` before Driver dispatch. The two admitted turns
returned `MODEL_MISMATCH` because the current Gemini UI menu exposed
`3.5 Flash-Lite`, `3.6 Flash`, and `3.1 Pro`, not the required `3.8 Flash +
強化版思考モード`. Do not silently downgrade the fixed model.

If changing this area again, preserve or re-prove at minimum:

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

Current implementation commits include Browser Chat Bridge `4ec9222`, Stable
Routing `a3a9333`, and DSH Browser Chat provider `176f3858cb`. Re-read current
files and do not assume these hashes remain HEAD.

Do not re-implement completed work. Continue only a currently failing or
explicitly requested acceptance item, then run the narrow relevant verification
and keep the working tree clean.

Use 100% to mean all requested implementation/review/deployment verification is
complete. Do not call the task 100% while a required live or fallback acceptance
gate is still unproven.

Do not start unrelated DSH routing work.

