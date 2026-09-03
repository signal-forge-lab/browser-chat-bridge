# DSH Browser Chat integration handoff — 2026-09-04

## Objective

Integrate the already-working Browser Chat Bridge into DeepSeek Harness so DSH
orchestration can use the browser-hosted Gemini UI as a governed execution
provider without weakening DSH routing, tool, cancellation, review, or retry
contracts.

The implementation must finish end-to-end: current-state discovery, architecture
decision, code, composition, tests, live DSH execution, independent review,
repairs, re-review, operational start/stop instructions, and final commits.

## Ground truth at handoff

### Browser Chat Bridge

Repository:

```text
C:\Users\shogo\Documents\Intelligence Works\github\browser-chat-bridge
```

Handoff commit:

```text
23af9e736148d92eb5427d8eee93455a8b611533
23af9e7 Add run-scoped browser chat bridge
```

The repository was clean at handoff. Re-read the current files and Git state;
newer commits override this cached description.

Implemented process boundary:

```text
caller
  -> Bridge  127.0.0.1:8765
  -> Driver  127.0.0.1:8766
  -> local Chromium CDP
  -> https://gemini.google.com/
```

Bridge and Driver are intentionally separate OS processes.

Relevant documents:

```text
README.md
docs/DESIGN.md
docs/GEMINI_DOM_CONTRACT_20260904.md
docs/OBSCURA_SHADOW_20260904.md
```

Live acceptance already proven through Chromium:

```text
run:          bcb-e2e12-run
turn 1:       BCB_E2E12_TURN1_OK
turn 2:       BCB_E2E12_TURN2_OK
conversation: 8cd9762cf08afb94 on both turns
duplicate request_id replay: cached=true, no extra DOM turn
model picker: Flash / 拡張
```

The fixed UI model is Gemini 3.8 Flash with 強化版思考モード. The DSH
integration must not create a user/model/workflow override that can silently
change that UI selection. The Driver remains the authority for proving and
repairing the fixed UI model before every send.

Bridge API used by callers:

```text
POST /v1/runs/{run_id}/turn
Content-Type: application/json

{
  "request_id": "unique browser-turn id",
  "prompt": "one new prompt only"
}
```

Successful reply:

```json
{
  "request_id": "...",
  "run_id": "...",
  "status": "COMPLETED",
  "content": "...",
  "conversation_id": "...",
  "conversation_url": "https://gemini.google.com/app/<id>",
  "error": null,
  "cached": false
}
```

The cloud conversation is the history authority. The caller sends only the new
prompt for each turn. Do not replay the complete DSH message history into an
existing Browser Chat run.

Dispatch safety is load-bearing. A live probe proved that a synthetic send may
persist late after appearing unsuccessful; a second blind attempt created two
identical User turns. `AMBIGUOUS` therefore never authorizes another send of the
same browser turn. Request IDs are idempotency keys.

### DeepSeek Harness

Repository:

```text
C:\Users\shogo\Documents\Intelligence Works\github\deepseek-harness
```

Observed handoff state:

```text
HEAD: b150a551b8d465e31e418e1b2eaf5e79bbb7d28e
      Merge pull request #2908 from deepseek-harness/release/dsh-0.1.1-rc.2
checkout: detached HEAD
```

Pre-existing unrelated untracked entries were present:

```text
?? $null
?? wf.runAgent({name
```

Do not stage, delete, rename, or otherwise absorb these files into this work
unless current inspection proves they belong to this task. Use a dedicated DSH
worktree/branch for implementation. Before coding, fetch upstream and determine
the current canonical base; do not assume the cached handoff commit is still
latest.

Relevant DSH seams already verified:

```text
packages/llm/llm/src/types.ts
  GenerateOptions has provider/model/messages/system/tools/sessionId/purpose.
  It does NOT carry WorkflowRunId.

packages/llm/llm/src/index.ts
  LlmAdapter is the provider-wire model adapter seam.

packages/subagent/subagent/src/types.ts
  SubagentProvider.start(ResolvedSubagentStartRequest) -> SubagentRun.
  One start is one disposable one-shot child run.

packages/subagent/subagent/src/index.ts
  ctx.subagents.registerProvider(provider) is the provider registry.
```

DSH package policy requires product-visible providers to have a non-unit REAL
composition test. Read `packages/AGENTS.md` and the current testing docs before
implementing.

### External Workflow deployment

Installed package inspected at:

```text
C:\Users\shogo\Documents\Intelligence Works\tools\dsh-workflow-iw
```

This directory is an installed/deployed package tree, not a Git checkout at
handoff. Do not edit compiled `lib/` as the source implementation. Find the
canonical source repository or change only deployment configuration when that
is all that is required.

Current important behavior:

- workflow-authored model hints are closed to `fast | balanced | deep`;
- current task input no longer accepts arbitrary workflow-authored
  provider/model fields;
- central model/routing policy remains outside workflow source;
- `WorkflowRun` has a durable `runId`;
- every workflow child is started through `ctx.subagents`, optionally via the
  deployment `WorkflowDispatchAdapter`;
- current engine builds the child request with prompt, parent, signal,
  maxTokens, optional toolFilter and optional outputSchema;
- current workflow task request does not put provider/model into
  `agentOptions` before the deployment dispatch seam;
- `fast` write-capable work is promoted to balanced by engine policy;
- deep verification may use centrally governed escalation/fallback.

Current deployment routing must remain authoritative. Workflow source must not
gain a new provider/model escape hatch.

## Architecture decision to prove before implementation

### Preferred first production seam: DSH SubagentProvider

The current Browser Chat contract is text-in/text-out and does not implement
DSH model tool-call streaming. Therefore the first architecture to prove is a
dedicated DSH subagent provider, tentatively named `browser-chat`, rather than a
generic `LlmAdapter` that pretends to be a fully tool-capable coding model.

Why this seam currently fits best:

1. `SubagentProvider.start()` already represents one one-shot run.
2. The Bridge already represents one run as one cloud conversation.
3. A remote provider can return assistant text without constructing a local
   DSH Agent.
4. Parallel workflow children naturally receive distinct Browser Chat threads.
5. The UI model stays fixed in Driver instead of becoming a workflow-authored
   model override.
6. No DSH tool-call capability needs to be fabricated.

Target identity mapping:

```text
one DSH SubagentProvider.start()
        <-> one provider-owned subagent run id
        <-> one Browser Chat Bridge run_id
        <-> one Gemini conversation
```

Do NOT map one top-level WorkflowRunId to one Gemini conversation when the
workflow can run several children in parallel. That would interleave unrelated
agents in one chat thread. The Browser Chat run belongs to the subagent run.

Inside a single subagent run, a provider-owned format/structured-output repair
may use a second Bridge turn with the SAME Bridge run_id. Distinct subagent
starts use distinct Bridge run_ids.

If current code inspection disproves this seam, document the exact contract
that fails and choose the smallest correct alternative. Acceptable alternatives
include a dedicated WorkflowDispatchAdapter or a no-tool LlmAdapter, but do not
broaden shared DSH interfaces merely for convenience. In particular, do not add
WorkflowRunId to generic `GenerateOptions` unless a current Consumer genuinely
requires that public contract and the review proves the wider change is worth
it.

## Provider contract

### Suggested package ownership

Prefer a DSH package in the existing subagent family, following current naming
and cookbook conventions, e.g. conceptually:

```text
packages/subagent/subagent-browser-chat/
```

The exact name must follow current repository naming/aggregate rules found at
implementation time.

### Configuration

Keep configuration minimal and deployment-owned. Expected needs:

```text
providerName: browser-chat
bridgeBaseUrl: http://127.0.0.1:8765
requestTimeoutMs: bounded value compatible with enhanced-mode latency
```

Default network authority must remain loopback. Do not add arbitrary remote
Bridge URLs unless a current deployment requirement proves the need and the
security review covers it.

Do not expose a DSH model-id setting that changes the Gemini UI model. The
Driver is fixed to 3.8 Flash + 強化版思考モード.

### Capabilities

Capability declarations must be truthful and proven by tests.

Expected starting point:

```text
inheritsParentContext: false
depthLimit:             false
persona:                false
```

`toolFilter` and `outputSchema` require explicit decisions:

#### toolFilter

The remote Browser Chat child has no DSH tool surface at all. It can truthfully
enforce a restriction only if the implementation guarantees that no DSH tools
are exposed or executable. If that satisfies the current DSH `toolFilter`
contract, advertise `toolFilter: true` and test that arbitrary restrictions do
not create tools. If the contract requires a filtered subset of an otherwise
real child tool registry, leave it false and do not route workflow tasks that
require it. Do not claim support merely to pass External Workflow preflight.

#### outputSchema

For useful Planner/Reviewer/Judge workflow roles, structured output is highly
valuable. Prefer implementing truthful provider-owned structured output if it
can be done within the current DSH schema helpers:

1. include a deterministic JSON-only instruction derived from the requested
   object schema;
2. send turn 1 through the Bridge;
3. strictly parse the returned text without brace-slicing/prose heuristics;
4. validate with DSH's canonical object-schema validator;
5. if invalid, send at most ONE format-repair prompt on the SAME Bridge run;
6. strictly parse and validate again;
7. success returns both text output and `structured`;
8. second failure returns `stopReason: error` with bounded diagnostic.

Never report invalid JSON as successful structured output. Never start a new
Bridge run for the repair because that loses the conversation context.

If canonical DSH policy makes this provider-side implementation incorrect,
leave `outputSchema: false`, prove the limitation, and keep the provider out of
roles that require structured output. Do not accept-and-ignore the field.

### Prompt framing

The provider receives `ResolvedSubagentStartRequest.prompt` as DSH content
blocks. Support exactly the modalities that can be faithfully represented by
the current Browser Chat Bridge. At v0.1 that is text only unless current Bridge
code has advanced.

Reject unsupported image/tool/persona/depth semantics before dispatch. Do not
silently stringify unknown content blocks.

Do not replay parent history. `inheritsParentContext=false` means the provider
uses the request prompt prepared by DSH as the new run's initial prompt.

### Run and request identity

Use a provider-owned unique run identity for each `start()`. It must also be a
valid remote `SubagentRun.id` in the parent namespace according to the current
DSH contract.

Suggested Bridge request IDs:

```text
<provider-run-id>:turn:1
<provider-run-id>:turn:2   # only for bounded structured repair
```

These IDs are stable for the life of that provider run and must not be reused
for a different prompt.

### Status mapping and retries

Read the current Bridge status vocabulary from code before implementation.
Known current statuses include completed and typed fail-closed states such as:

```text
COMPLETED
NOT_DISPATCHED
AMBIGUOUS
TARGET_LOST
AUTH_REQUIRED
MODEL_MISMATCH
BUSY
CONVERSATION_LOST
CONVERSATION_MISMATCH
TIMEOUT
```

Rules:

- `COMPLETED` is the only success.
- no status with missing/ambiguous content becomes `completed`.
- `AMBIGUOUS` never causes the provider to resend the same Bridge turn.
- provider transport loss after a possibly-side-effecting Bridge dispatch is
  treated as ambiguous unless the Bridge idempotency result is safely
  reconciled.
- route-level Stable Routing fallback is a separate policy decision from
  resending a Browser Chat request. Integrate with existing failure
  classifications; do not create a hidden provider-specific retry loop.
- diagnostics must not contain prompts, full responses, cookies, credentials,
  raw protocol payloads, or sensitive filesystem data.

### Cancellation and disposal

Trace the current DSH `SubagentRun` contract before coding. Cancellation is not
optional.

The provider must honor the request `AbortSignal`, return a legal aborted/error
result, and make `dispose()` idempotent. If the current Bridge has no safe
generation-cancel API, do not pretend an in-flight browser generation stopped.
The minimum correct behavior is to stop publishing work to DSH and await or
otherwise reconcile the in-flight Bridge operation before claiming provider
quiescence. Add a Bridge/Driver cancel seam only if it can be implemented and
live-verified without introducing resend or cross-run hazards.

Document the exact chosen behavior and test abort-before-dispatch,
abort-after-dispatch, double-dispose, and transport loss.

## DSH composition and routing

Register the provider through the current DSH plugin/composition mechanism and
add the package to every required aggregate/reference according to current repo
policy. Do not hand-edit generated artifacts.

Do not change existing fast/balanced/deep priority or fallback order merely to
prove the provider works. First make the provider available and live-verified.
Then inspect the user's current central Stable Routing/deployment configuration.
If an existing policy clearly defines where a new provider candidate belongs,
add it there without creating workflow-authored override fields. Otherwise leave
route priority unchanged and document the exact config line needed to opt in.

When External Workflow selects Browser Chat as its subagent transport, the
matching tier must not also inject an unrelated DSH LLM provider/model that the
Browser Chat provider would ignore. Accepted fields must have effects.

## Operational process work

Browser Chat Bridge and Driver remain separate processes. The DSH provider must
not silently embed the browser driver into the DSH process.

Before calling the integration production-ready, provide a minimal one-command
local operating path for the Bridge repo:

- start Bridge and Driver as separate processes;
- status/health check;
- stop both cleanly;
- no Windows Task Scheduler requirement;
- no browser-profile/cookie/credential files committed;
- runtime PID/log/database data remains ignored;
- existing user-owned Chromium tabs are not focused or closed by routine
  provider lifecycle.

Use existing repo/process conventions if they already solve this; do not build a
second supervisor unnecessarily.

## Required test matrix

### Browser Chat Bridge regression

Run its full current unit suite plus any new tests required by DSH integration.
Current baseline at handoff was 9/9 unittest PASS.

### DSH provider unit tests

At minimum prove:

1. provider registration/disposal is HMR-safe;
2. one start mints one unique Bridge run;
3. parallel starts use distinct run IDs;
4. request prompt conversion is exact for supported text;
5. unsupported request capability fails before Bridge dispatch;
6. `COMPLETED` maps to `SubagentResult.stopReason=completed`;
7. typed Bridge errors map to non-completed outcomes;
8. `AMBIGUOUS` produces zero resend attempts;
9. duplicate provider/transport failure cannot double-send one request_id;
10. abort-before-dispatch performs zero Bridge turn calls;
11. abort-after-dispatch follows the documented quiescence policy;
12. dispose is idempotent;
13. structured-output valid first response passes, if supported;
14. invalid first structured response gets at most one same-run repair, if
    supported;
15. invalid repair fails, never reports success;
16. diagnostics are bounded and redact request/response bodies.

### REAL composition test

Required by `packages/AGENTS.md`. Boot a real test composition through DSH's
Loader/app/process path. Mock only the external Browser Bridge boundary. Assert
the model/user-visible or durable provider result, not merely internal method
calls.

### Live DSH smoke

With the actual Bridge/Driver and authenticated Chromium session:

1. DSH starts one Browser Chat-backed provider run.
2. The exact Browser Chat conversation is newly created for that run.
3. The DSH result returns the expected text.
4. A second distinct DSH provider run uses a different conversation.
5. If structured repair is implemented, force one invalid-format first answer
   in a safe test and prove turn 2 reuses the same conversation.
6. Retry/idempotency evidence shows no duplicate user turn.

### Live External Workflow smoke

After provider composition is proven, run the smallest workflow that can
truthfully use this provider under current capability rules. Do not weaken
workflow preflight to make the smoke pass.

Prove:

- workflow run starts;
- Browser Chat-backed child starts through central provider/routing policy;
- child result returns to workflow;
- workflow reaches a legal terminal state;
- parallel Browser Chat children, if tested, use distinct conversations;
- no workflow-authored provider/model override was introduced.

## Independent review and repair gate

Implementation is not complete after tests turn green.

Run an independent deep review against BOTH repositories and the effective DSH
deployment/config. Review at least:

1. correctness and lifecycle;
2. DSH package/service architecture;
3. retry/idempotency and ambiguous-send safety;
4. cancellation/quiescence;
5. capability truthfulness;
6. structured-output validation;
7. security/loopback trust boundary;
8. secret/log privacy;
9. parallel-run isolation;
10. Stable Routing authority and no override escape hatch;
11. test adequacy, including REAL composition;
12. operational start/stop behavior;
13. over-engineering/dead flexibility.

Any blocking finding must be repaired, the relevant focused/full tests rerun,
and the independent review repeated. Finish with a separate final judge/reviewer
that did not author the implementation.

## Completion criteria

Do not report 100% until all applicable items are evidenced:

- current upstream/base checked before implementation;
- dedicated worktrees/branches used for code modifications;
- DSH integration seam selected from actual contracts and documented;
- provider implemented without fake capabilities;
- Bridge/Driver process separation preserved;
- run/thread identity proven under parallel use;
- no blind resend after ambiguous dispatch;
- DSH focused tests PASS;
- DSH full required gate PASS or every unrun gate is explicitly justified;
- REAL composition test PASS;
- Browser Chat Bridge tests PASS;
- actual Chromium Bridge health PASS;
- live DSH provider E2E PASS;
- live External Workflow smoke PASS when capability-compatible;
- independent review completed;
- all blocking findings repaired and re-reviewed;
- docs/config/run instructions updated;
- Git diff/status reviewed;
- task commits created in the correct repositories without unrelated files;
- production processes left in a known state and Shadow/test-only processes
  stopped;
- final report states exact commits, tests, live evidence, remaining limitations,
  and progress=100% only when no AI-executable requested work remains.

## Explicit non-goals unless forced by evidence

- Do not add OpenAI-compatible façade semantics merely to fit an existing
  adapter.
- Do not implement generic DSH tool calling through Gemini UI in this task.
- Do not merge Bridge and Driver into one process.
- Do not move cloud conversation history into DSH or replay full history every
  turn.
- Do not make Obscura production-authoritative; current Shadow evidence remains
  a separate compatibility track.
- Do not expose arbitrary workflow provider/model overrides.
- Do not modify unrelated pre-existing DSH untracked files.
