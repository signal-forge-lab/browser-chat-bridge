# Browser Chat DSH provider — frozen implementation plan (2026-09-04)

Status: contract frozen from current files / Git / process / effective-profile / CDP
truth, plus the three Phase‑1 audits (dsh-seam-investigator, workflow-routing-
investigator; bridge-contract lane was absorbed from the other two plus direct
source reads below). Product code follows this document. The companion staging
branch `dsh-browser-chat-staging` (worktree `.worktrees/dsh-browser-chat-staging`
in this repository) carries the ready-to-apply DSH change set and the Bridge
operating scripts; see §10 for why the DSH repository itself could not be written
from this session and exactly how to apply the staged tree.

## 1. Current-state truth (verified in this session, not from cache)

| Target | Verified state |
|---|---|
| Bridge repo | `main` @ `1708c5c` (docs-only on top of handoff `23af9e7`), working tree clean; `.runtime/*`, `__pycache__` ignored; `cookies.json` present only under ignored `.runtime/obscura-gemini/` |
| Bridge API | `POST /v1/runs/{run_id}/turn` `{request_id, prompt}`; global `request_id` idempotency bound to (run_id, sha256(prompt)) — replay returns `cached:true`; `GET /health`; loopback-only host enforcement; no cancel API |
| Bridge statuses | `COMPLETED`, `NOT_DISPATCHED`, `AMBIGUOUS`, `TARGET_LOST`, `AUTH_REQUIRED`, `MODEL_MISMATCH`, `BUSY`, `CONVERSATION_LOST`, `CONVERSATION_MISMATCH`, `TIMEOUT`, HTTP 409 `REQUEST_CONFLICT`; driver-transport loss ⇒ `AMBIGUOUS`; completed without durable conversation binding ⇒ `AMBIGUOUS`; per-run conversation binding is durable (sqlite), mismatch fails closed |
| Driver | fixed model **Gemini 3.8 Flash + 強化版思考モード** verified/selected before every send (`MODEL_MISMATCH` otherwise); post-click uncertainty is always `AMBIGUOUS`; CDP `Target.createTarget` background=true protects user tabs; promotion wait default 120 s; response timeout default 360 s |
| Live processes | Bridge `127.0.0.1:8765/health` ⇒ `{"ok":true,"driver_url":"http://127.0.0.1:8766"}`; Driver `:8766/health` ⇒ `{"ok":true,"backend":"chromium","cdp_endpoint":"http://127.0.0.1:51881","fixed_model":"Gemini 3.8 Flash / 強化版思考モード"}`; **CDP `:51881` currently refuses connections** (no Chromium CDP listener running — a dispatch now would yield `TARGET_LOST`); the two Python servers are running |
| DSH checkout | `C:\...\github\deepseek-harness`, detached HEAD `b150a551` (= `dsh-v0.1.1-rc.2`), one worktree (the checkout itself), unrelated untracked `$null` and `wf.runAgent({name` present and untouched. Local tags include `dsh-v0.1.2-alpha.1`; a `git fetch` was impossible from this session (see §10), so upstream freshness beyond local tags is unverified. All 230 workspace package versions pin `0.1.1-rc.2` ⇒ the effective deployment base is `b150a551`; the provider targets that base |
| External Workflow | deployed tree `tools/dsh-workflow-iw` (`@dsh-external/workflow` 0.1.2-iw.1, build output, not Git source — never edit `lib/`); engine admission: exact-key task whitelist (no `provider`/`model` keys possible), `modelHint ∈ {fast,balanced,deep}`, `readOnly ⇒ capabilities.toolFilter`, `outputSchema ⇒ capabilities.outputSchema`, `subagentType` validated against the central registry; children are built with `prompt/parent/signal/agentOptions{maxTokens?}/toolFilter?/outputSchema?` only |
| Stable Routing | deployment-owned `model-policy.json` (max7 candidates/tier, circuits, phase pinning) + dispatch adapters; both deployed adapters inject `agentOptions.provider/model` for LLM transports (v45 *requires* them, current engine never sends them — pre-existing deployment incompatibility, out of scope here); workflow source cannot select provider/model; `tool-workflow` disabled in the workflow bundle patch precisely to prevent routing bypass |
| Profiles | 11 profiles under `~/.dsh/profiles/*`; production pair `headless/web-dual-provider-production`; Goal Certification is runtime-owned (`packages/goal`, `tool-goal`, `goal-round-driver`) — workflow/planner sources create no Goal Reviewer/Repair/Re-review/Final-Judge tasks |
| Bridge tests | `python -m unittest discover -s tests`: 5/9 pass in this sandbox; the 4 store-backed tests fail only because this session's file sandbox denies writes inside `tempfile.TemporaryDirectory` (proven by direct probes: plain `write_text` denied there, while `sqlite3` + `BridgeStore` with WAL succeed at `.runtime/` paths). Code is byte-identical to the handoff-verified 9/9 state (clean tree) — no regression |
| DSH contracts read in full | `packages/subagent/subagent/src/{types,index,out-of-process}.ts`; templates `subagent-acp/src/{index,run}.ts`, `subagent-codex/src/{index,run,wire,invariant}.ts` + all its tests; `packages/core/tools/src/json-schema.ts` (`assertObjectJsonSchema`, `validateJsonSchemaValue`); wiring `tsconfig.host.json` (subagent block ends line 276), `knip.json` (package entries; examples fixture entries lines 63–71), `scripts/gen-doc-graphs.ts` (lines 389/486), `packages/subagent/README.md`(+zh), `vitest.config.ts` (`packages/*/*/tests/**/*.spec.ts` auto-included), loader fixture `examples/acp-agent/tests/fixtures/subagent/subagent-codex/*` |

## 2. Architecture decision (frozen)

**Dedicated DSH `SubagentProvider` package `packages/subagent/subagent-browser-chat`
(`@deepseek-ai/dsh-subagent-browser-chat`)**, an optional Profile Bundle function
plugin modeled on `subagent-codex` (wire-driven remote provider; no subprocess
ownership — the Bridge is an already-running separate process).

- `SubagentProvider.start()` = one disposable one-shot run = **one Bridge `run_id`
  = exactly one Gemini conversation** (first completed turn binds durably;
  conversation never crosses runs; `CONVERSATION_MISMATCH` enforced Bridge-side).
- Parallel starts mint distinct `SessionId(randomUUID())`s ⇒ distinct runs ⇒
  distinct conversations. One top-level WorkflowRunId is never mapped to one
  conversation.
- `localAgent: undefined`; no local DSH Agent, no DSH tools fabricated.
- `LlmAdapter` rejected for v1 (again, from current source): `GenerateOptions`
  carries no run identity and presumes fully-assembled tool-capable model
  requests. No shared DSH interface is widened; no current consumer requires it.
- Bridge/Driver remain separate OS processes; the provider is only an HTTP
  client to a loopback Bridge. The Driver keeps sole authority for the fixed
  Gemini 3.8 Flash + enhanced-thinking selection; the provider exposes **no
  model/user/workflow model fields** whatsoever.

## 3. Frozen provider contract

### 3.1 Capabilities (truthful, tested)

```text
outputSchema: true    # provider-owned structured output via DSH canonical validators
depthLimit:   false
toolFilter:   true    # guaranteed absence: zero DSH tools exist or execute in the child
persona:      false
inheritsParentContext: false   # descriptive; prompt is the DSH-prepared request text
```

- `toolFilter: true` truthfulness argument: the remote Browser Chat child exposes
  and executes **zero** DSH tools — there is no tool registry to subset. Any
  `ToolRestriction` is enforced absolutely (restriction ⇒ still zero tools; no
  accept-and-ignore). Test proves arbitrary restrictions create no tools and
  dispatch exactly the same single turn. This is what makes readOnly workflow
  tasks dispatchable (engine rejects `readOnly` without `capabilities.toolFilter`).
- `outputSchema: true` implementation uses DSH's canonical
  `assertObjectJsonSchema` (pre-validated by `SubagentRuntime.start`) and
  `validateJsonSchemaValue` from `@deepseek-ai/dsh-tools` — the same validators
  the workflow engine re-applies, so provider-validated `structured` values can
  never diverge from engine re-evaluation, and the engine's own repair path
  (which starts a *new* child = new conversation) can never fire for a
  provider-validated value (`structuredEvaluation` trusts `outcome.structured`).
- No `prepareContinuable` (method presence IS that capability; omitted).

### 3.2 Run and request identity

- `SubagentRun.id = SessionId(randomUUID())`; the same string is the Bridge
  `run_id`. One identity, zero mapping, collision-free in the parent namespace.
- Bridge `request_id`s are deterministic for the life of the run and never
  reused for a different prompt:
  `${runId}:turn:1` — the task turn; `${runId}:turn:2` — only for the single
  bounded structured repair on the same run/conversation.
- Duplicate delivery of the same `request_id` is reconciled by Bridge
  idempotency (`cached:true`), never by the provider re-sending a new prompt.

### 3.3 Prompt conversion (fail loud before dispatch)

- Accept exactly a non-empty sequence of `text` content blocks; join with
  `\n\n`. Any non-text block (image/tool-result/persona/etc.) or an
  all-whitespace prompt ⇒ `start()` rejects **before any Bridge call**.
- `label` is display metadata persisted in the descriptor; not a capability, not
  forwarded.
- No parent history is replayed: the cloud conversation is the history
  authority and receives only the new prompt.

### 3.4 `agentOptions` policy (no silently ignored accepted fields)

- Absent ⇒ fine. Present ⇒ own keys must be a subset of `{maxTokens}`;
  `maxTokens` must be a positive safe integer.
- Any other key — centrally injected LLM routes (`provider`, `model`) included —
  fails loud pre-dispatch, naming only the offending key **names** (never
  values). Consequence (deliberate): do **not** wire `browser-chat` into an
  LLM model tier in v1 (`*Provider: browser-chat`); the tier would name an LLM
  route this provider cannot honor. Opt in via `subagentType: browser-chat` in
  a workflow task or a direct `tool-subagent` row — see §6.
- `maxTokens` has a real, deterministic effect: a fixed output-bound sentence
  appended to the prompt (`Keep the final answer within approximately N tokens.`).
  It is a prompt-space bound, documented and tested as such — not a hard
  truncation guarantee (see Known Limitations in the package README).

### 3.5 Status / error / AMBIGUOUS mapping (fail closed; `COMPLETED` is the only success)

| Bridge outcome | `SubagentResult` |
|---|---|
| `COMPLETED` + non-empty content | `stopReason: 'completed'`, `output: [{type:'text',text}]` (+ `structured` when requested and valid) |
| `COMPLETED` with empty/missing content | `error` — fail closed, never success |
| `TIMEOUT` | `error` (confirmed user turn, response never reached structural completion) |
| `NOT_DISPATCHED`, `TARGET_LOST`, `AUTH_REQUIRED`, `MODEL_MISMATCH`, `BUSY`, `CONVERSATION_LOST`, `CONVERSATION_MISMATCH` | `error`, fixed safe diagnostic naming the status (+ the Bridge's own bounded safe `error` sentence, type-names/fixed prose only) |
| `AMBIGUOUS` (any origin) | `error`, diagnostic suffixed `ambiguous dispatch; not resent` — **zero resend attempts, ever** |
| HTTP 409 `REQUEST_CONFLICT` / non-200 / transport loss / invalid body after the request was sent | `error`, diagnostic suffixed `treated as ambiguous; not resent` — no transport-loss retry exists in v1 (deferred: one bounded same-`request_id` reconcile replay, §11) |
| request `AbortSignal` / dispose | `aborted` |

- No `refusal`/`max-tokens` stop reasons are produced by this provider.
- Diagnostics are provider-authored fixed facts (status word, stage, run id,
  first schema-violation string) passed through `limitSubagentDiagnostic`
  (≤4096 UTF-8 bytes). Never included: prompts, model replies, cookies,
  credentials, raw HTTP bodies, URLs with credentials, filesystem paths of the
  caller.

### 3.6 Structured output (optional strict, bounded to one same-run repair)

1. If `outputSchema` is requested, append a deterministic JSON-only instruction
   to the task text: the fixed sentence demanding *only* a single JSON object
   conforming to the schema, followed by `JSON.stringify(schema, null, 2)`.
2. Send turn 1 (`:turn:1`).
3. Strict parse: trim outer whitespace, `JSON.parse` the **whole** text; no
   brace-slicing, no fence stripping, no prose heuristics. Root must be an
   object. Then `validateJsonSchemaValue(schema, value)` must return `[]`.
4. On failure, send exactly one fixed repair prompt on the **same run**
   (`:turn:2`; the invalid answer is already in the conversation context, so the
   repair prompt never echoes prompts or replies). Fixed text: reply again with
   only the JSON object, no other text.
5. Strict parse + validate again. Success ⇒ `completed` with `output` = the
   final validating text and `structured` = the validated value.
6. Second failure ⇒ `error` with bounded diagnostic (stage `repair`, first
   violation). Invalid JSON is never reported as successful structured output.
   No third turn; no new Bridge run is ever started for repair.
7. A non-`COMPLETED` Bridge status on turn 1 or the repair turn follows §3.5
   mapping directly (no repair attempted after transport-class failures).

### 3.7 Abort / quiescence / dispose (no Bridge cancel API exists)

- Aborted before `start()` fulfills ⇒ reject after cleaning partial state; zero
  Bridge calls.
- Aborted after publication ⇒ `result` settles locally `aborted` (race, codex
  pattern); the run never initiates the repair turn afterwards.
- Quiescence truth: aborting does **not** claim the browser generation stopped.
  The run's only in-flight artifact is the HTTP request; `dispose()` aborts that
  client-side request, awaits settlement of the (already-cancelled) result, and
  is memoized/idempotent. The Bridge/Driver processes — separate by design —
  may finish recording the turn on their own schedule; deterministic request
  ids keep any late completion reconcilable via replay, which the provider
  itself never performs automatically.
- Tested: abort-before-dispatch (0 Bridge calls), abort-after-dispatch
  (`aborted`, no repair, no extra calls), double-dispose (same memoized
  promise), transport loss (0 resends).

### 3.8 Configuration (schemastery; loopback authority)

| Key | Default | Validation / meaning |
|---|---|---|
| `providerName` | `browser-chat` | non-empty registry name; duplicate registration fails loud (`DUPLICATE_PROVIDER`) |
| `bridgeBaseUrl` | `http://127.0.0.1:8765` | URL-parsed at load; only `http` with hostname `127.0.0.1` / `::1` / `localhost`, no userinfo, empty-or-`/` path; anything else — including any remote host — rejects at plugin load. No config path exists to widen this |
| `requestTimeoutMs` | `480000` | positive finite ≤ `MAX_TIMER_DELAY_MS`; per-turn client bound, deliberately above the Bridge's own 420 s driver wait so Bridge-authoritative statuses (incl. `TIMEOUT`) win over the client bound |

No model, user, persona, cwd, env, or remote-URL fields. Loading the plugin
probes nothing and starts nothing (dormant provider; `starts: 0` asserted by the
Loader composition test).

### 3.9 HMR / registration

Effect-scoped `ctx.subagents.registerProvider(...)` via `ctx.effect` inside the
registry (existing semantics): fiber dispose removes the provider; accepted runs
stay holder-owned. Test asserts add/remove events and `NO_PROVIDER` after
removal. Plugin keeps the named-export shape (`name`/`inject`/`Config`/`apply`,
no default export — postmortem 0001) and `inject: ['subagents']` only.

## 4. Package plan (DSH repo)

```text
packages/subagent/subagent-browser-chat/
  package.json        # @deepseek-ai/dsh-subagent-browser-chat @ 0.1.1-rc.2,
                      # publishConfig public, files: lib/index.js, lib/invariant.js,
                      # cordis.patch.yml, lib/types/**/*.d.ts; dsh.bundle.patch = ./cordis.patch.yml
                      # peers: cordis, dsh-invariants, dsh-llm, dsh-session, dsh-subagent,
                      #        dsh-timeout, dsh-tools; deps: schemastery; dev mirror + loader-smoke
  tsconfig.json       # extends tsconfig.base.json; refs vendor×3, core/agent, llm/llm,
                      # core/session, core/tools, subagent, util/timeout, runtime-diagnostics/invariants
  cordis.patch.yml    # insert: [{id: subagent-browser-chat, name: '@deepseek-ai/dsh-subagent-browser-chat'}]
  src/index.ts        # Config + apply + BrowserChatProvider (capabilities §3.1)
  src/run.ts          # startBrowserChatRun: admission, identity, turns, mapping, repair, settle
  src/invariant.ts    # package-owned invariant companion (no runtime invariant: reasons)
  tests/bridge-fixture.ts            # loopback mock Bridge (node:http): health, idempotent replay,
                                     # prompt-hash conflict, scripted statuses, request journal
  tests/subagent-browser-chat.spec.ts # unit+product matrix §5
  tests/loader-composition.e2e.ts     # REAL composition (Loader boot; providers/tools/start:0)
  tests/live-bridge.e2e.ts            # LIVE smoke vs real Bridge; self-skips unless
                                      # DSH_BROWSER_CHAT_LIVE_BRIDGE=1
  README.md / README.zh.md            # canonical Model Experience + Known Limitations sections
examples/acp-agent/tests/fixtures/subagent/subagent-browser-chat/
  fixture.ts / cordis.yml / driver.ts # loader fixture (mirrors subagent-codex; no subprocess row)
```

Wiring edits (exact, minimal):

1. `tsconfig.host.json`: add `{ "path": "./packages/subagent/subagent-browser-chat" }`
   after the `subagent-dsh-sdk` line (276) — the single aggregate registration.
2. `knip.json`: package entry (`tests/**/*.spec.ts`, `tests/**/*.e2e.ts`, +
   `tests/bridge-fixture.ts` as entry) and two fixture entries under the
   `examples` workspace (`.../subagent-browser-chat/fixture.ts`, `driver.ts`).
3. `scripts/gen-doc-graphs.ts`: append `subagent-browser-chat` to the provider
   arrays at lines 389 (consumers) and 486 (implementations), then regenerate
   `docs/config-catalog.md`(+zh), `docs/module-graph.md`, `docs/capability-seams.md`
   via `pnpm run doc-sync` — never hand-edit generated docs.
4. `packages/subagent/README.md` + `README.zh.md`: one table row
   (`subagent-browser-chat/` — Starts a Browser Chat Bridge conversation child).
5. `pnpm install` regenerates `pnpm-lock.yaml`; `README.i18n.yaml` is recorded by
   `pnpm run verify-translation-pairing --write packages/subagent/subagent-browser-chat/README.md`.
6. Agent Note in the same change: `.agents/notes/implemented/feature/2026-09-04-subagent-browser-chat-provider.md`.

## 5. Test matrix (maps 1:1 to the handoff's 16 required proofs)

Unit+product spec (mock Bridge on loopback; real `Context` + `SubagentRuntime`
+ plugin; starts through `ctx.subagents.start('browser-chat', …)`):

1. registration/disposal HMR-safe; namespace shape (no default export,
   `unwrapExports` identity); duplicate name rejection; config validation
   (loopback enforcement incl. remote-host rejection; timeout bounds);
2. one start ⇒ one unique Bridge run (`/v1/runs/<runId>/turn` observed; uuid
   distinct per start);
3. parallel starts ⇒ distinct run ids and distinct conversations (fixture
   binds per-run conversation ids; also proves run↔conversation isolation);
4. exact text conversion (joined `\n\n`, verbatim in the request body);
5. non-text block / empty prompt / unsupported `agentOptions` keys reject
   before any Bridge call (request count 0);
6. `COMPLETED` ⇒ `completed` + exact output;
7. every typed status ⇒ `error` + safe diagnostic;
8. `AMBIGUOUS` ⇒ zero resend attempts (journal length);
9. transport loss after dispatch ⇒ `error`, zero resends, one request only;
10. abort-before-dispatch ⇒ zero Bridge calls;
11. abort-after-dispatch ⇒ `aborted`, no repair turn, documented quiescence;
12. `dispose()` idempotent (double await, same promise; no extra Bridge calls);
13. structured valid first reply ⇒ `completed` + `structured` validated;
14. invalid first reply ⇒ exactly one same-run repair (`:turn:2` on the same
    run path, same conversation id from fixture);
15. invalid repair ⇒ `error`, never success, exactly 2 turns total;
16. diagnostics bounded (≤4096 bytes) and free of prompt/response sentinels.

REAL composition (`loader-composition.e2e.ts`, mandated by `packages/AGENTS.md`):
boots the test-only `cordis.yml` through the Loader + app/process with the
package's real Bundle patch; asserts registered provider with the exact
truthful capabilities, the bound `subagent_browser_chat` tool schema, and
`starts: 0` — loading never touches the network.

LIVE smoke (`live-bridge.e2e.ts`, self-skipping): requires the real
Bridge+Driver+authenticated Chromium; asserts (a) run A ⇒ conversation X,
(b) run B ⇒ conversation Y ≠ X, (c) replaying run A's `:turn:1` returns
`cached:true` with identical content (no duplicate user turn), (d) health
precondition. Sentinel prompt is fixed and harmless.

Full repository gates for the applying session: `pnpm install`, `pnpm run
doc-sync`, `pnpm run constraints && pnpm run typecheck && pnpm run lint`,
`pnpm run build && pnpm run hygiene`, focused vitest run of the package, and
the CI coverage gate (per-file 100 % on `packages/*/*/src`).

## 6. Deployment posture (Stable Routing authority preserved)

- **No tier/priority/fallback changes.** `browser-chat` is registered as a
  dormant Host provider only. Route tables, `model-policy.json`, and both
  dispatch adapters are untouched.
- Opt-in today (documented, not applied): a workflow task with
  `subagentType: browser-chat` (passes preflight for readOnly and
  outputSchema tasks per §3.1), or a deployment `tool-subagent` row
  (`provider: browser-chat`, `toolName: subagent_browser_chat`,
  `backgroundMode: one-shot`, `maxDepth: provider-managed`) in a Profile patch.
- Do **not** configure `*Provider: browser-chat` model tiers in v1: the
  dispatch adapters inject `agentOptions.provider/model` for LLM transports and
  the provider fail-louds on them by design (§3.4). Tier wiring waits until the
  deployment grows a transport-aware tier or a model-less subagent-provider
  tier (central-policy change, deployment-owned).
- Workflow source gains no provider/model escape: no new task field, no
  `exactKeys` widening, no engine edit. `modelHint` stays closed;
  `tool-workflow` stays disabled.
- Planner/workflow sources create no Goal Reviewer/Repair/Re-review/Final-Judge
  tasks — Goal Certification remains runtime-owned (`packages/goal`,
  `tool-goal`, `goal-round-driver`).

## 7. Operating commands (Bridge repo; one command each)

New `scripts/` in the Bridge repo (staged on the integration branch):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1    # Driver + Bridge as separate
                                                              # background processes, PID files in .runtime/
powershell -ExecutionPolicy Bypass -File scripts/status.ps1   # Bridge/Driver /health + CDP reachability
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1     # stops exactly the recorded PIDs
```

- Start sets `CHAT_DRIVER_CDP_ENDPOINT` (default `http://127.0.0.1:51881`) and
  `CHAT_DRIVER_BACKEND=chromium`; refuses to start duplicates when healthy.
- Stop kills only the PIDs it recorded — never scans for, focuses, or closes
  any user-owned Chromium/tabs; Chromium with `--remote-debugging-port` is
  started by the user (their authenticated profile), not by these scripts.
- No Task Scheduler; `.runtime/` (PID files, logs, sqlite) stays ignored; no
  cookie/credential file is ever read or committed by the scripts.

## 8. Acceptance summary

| Gate | Command | Status from this session |
|---|---|---|
| Bridge unit suite | `python -m unittest discover -s tests` | 5/9 pass; 4 blocked by session-sandbox temp-dir denial (§1) — code identical to 9/9-verified handoff |
| Bridge/Driver health | `GET :8765/health`, `GET :8766/health` | PASS (live) |
| CDP target | `GET :51881/json/version` | currently refused — bring up Chromium before LIVE smoke |
| DSH focused tests | `pnpm vitest run packages/subagent/subagent-browser-chat` | staged; run after §10 apply |
| DSH full gates | `pnpm run doc-sync && pnpm run constraints && pnpm run typecheck && pnpm run lint && pnpm run build && pnpm run hygiene` + coverage | staged; run after §10 apply |
| REAL composition | loader e2e above | staged; run after §10 apply |
| LIVE provider E2E | `DSH_BROWSER_CHAT_LIVE_BRIDGE=1 pnpm vitest run .../live-bridge.e2e.ts` | staged; requires Chromium CDP up |
| External Workflow smoke | smallest capsule: one `classify-and-act`-style task, `readOnly: true`, `subagentType: browser-chat`, no authored verification | defined; run by the applying session after provider is live (central routing untouched) |

## 9. Explicit non-goals (unchanged from handoff)

No OpenAI-compatible façade; no DSH tool-calling through the Gemini UI; no
Bridge/Driver merge; no history replay into DSH; Obscura stays shadow; no
workflow provider/model overrides; unrelated DSH untracked files untouched; no
push/publish/release/PR; installed `tools/dsh-workflow-iw/lib` never edited as
source; no generic interface widening (no `WorkflowRunId` on
`GenerateOptions`, no new `SubagentProvider` members).

## 10. Execution constraint and apply procedure (read first)

This session's file sandbox is workspace-write scoped to the Bridge repository
only; a direct write probe against the DSH checkout was denied
(`Access to the path …deepseek-harness\.dsh-write-probe.tmp is denied`) and
approval escalation is disabled. `git fetch` in that clone (writes to `.git`)
and profile edits under `~/.dsh` are equally out of scope. Therefore the DSH
change set is staged **complete and exact** on branch
`dsh-browser-chat-staging` (worktree `.worktrees/dsh-browser-chat-staging` in
this repo) under `dsh-integration/`, and the delegating agent applies it:

1. `git -C C:\...\github\deepseek-harness worktree add -b dsh-browser-chat-provider ..\deepseek-harness-browser-chat b150a551`
   (dedicated worktree; the detached checkout, its untracked files, and the
   pinned tag stay untouched; check upstream freshness first if network is
   available — local `dsh-v0.1.2-alpha.1` exists but the deployment base is rc.2).
2. Copy `dsh-integration/package/` → `packages/subagent/subagent-browser-chat/`;
   `dsh-integration/fixture/` → `examples/acp-agent/tests/fixtures/subagent/subagent-browser-chat/`;
   `dsh-integration/agent-note/…md` → `.agents/notes/implemented/feature/`.
3. Apply the four wiring edits exactly as specified in `dsh-integration/README.md`
   (tsconfig.host.json, knip.json ×3 entries, gen-doc-graphs.ts ×2 arrays,
   packages/subagent README ×2 rows).
4. `pnpm install`; `pnpm run verify-translation-pairing --write packages/subagent/subagent-browser-chat/README.md`;
   `pnpm run doc-sync`; then the §8 gates; then commit on the worktree branch.
5. Bring up Chromium CDP + Bridge/Driver (`scripts/start.ps1`, `status.ps1`)
   and run the LIVE e2e; then the §8 External Workflow smoke.

## 11. Limitations and deferred work (deliberate, documented)

- No Bridge cancel API ⇒ abort stops the provider side only; the browser
  generation may complete in the Bridge processes (§3.7). A cancel seam remains
  future Bridge work, only if it can avoid resend/cross-run hazards.
- No transport-loss reconcile replay in v1 (conservative zero-resend);
  deferred: one bounded same-`request_id` replay that can only ever return the
  Bridge's cached row.
- `maxTokens` is a prompt-space bound, not an enforced token ceiling.
- Single-turn (+1 repair) design: no continuation/resume/streaming/progress.
- Diagnostics carry status/stage/run-id/violation facts only — deliberately
  coarse for privacy; no retry taxonomy is exposed to Stable Routing beyond
  ordinary `error` results (route-level fallback remains the deployment's
  separate policy decision).
- External Workflow tier wiring intentionally deferred (§6).
