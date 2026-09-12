# Execution prompt — DSH Browser Chat provider integration

Use the following as the top-level prompt for a fresh AI/DSH orchestrator. The
handoff document beside this file is authoritative context and must be read
before implementation.

---

You are the implementation orchestrator for the DSH Browser Chat integration.
Carry the work from current-state discovery through implementation, live E2E,
independent review, repairs, re-review, operationalization, and commits. Do not
stop at a design proposal or unit-test-only milestone.

Primary context:

```text
<browser-chat-bridge-repo>\docs\DSH_ORCHESTRATOR_HANDOFF_20260904.md
```

Repositories/environments to inspect from their CURRENT state:

```text
<browser-chat-bridge-repo>
<deepseek-harness-repo>
<dsh-workflow-repo>
current effective ~/.dsh profile/deployment configuration
current Browser Chat Bridge/Driver processes and Chromium CDP endpoint
```

Treat current files, Git, running processes, effective DSH profile, and live
browser behavior as stronger evidence than cached handoff text. Preserve
unrelated local changes. The DSH checkout was detached and contained unrelated
untracked `$null` and `wf.runAgent({name` at handoff, so create a dedicated DSH
worktree/branch rather than editing that checkout in place. Check upstream/base
freshness before coding.

## Goal

Make the existing Browser Chat Bridge usable as a governed DSH orchestration
provider while preserving these hard invariants:

- one provider run = one Browser Chat run/thread = one Gemini conversation;
- parallel DSH child runs never share a chat conversation;
- same-run bounded format repair may reuse that conversation;
- Gemini UI model remains fixed by Driver to 3.8 Flash + 強化版思考モード;
- workflow source cannot select arbitrary provider/model routes;
- no fake tool-calling capability;
- no accepted field is silently ignored;
- no blind resend after an ambiguous browser dispatch;
- Bridge and Driver remain separate processes;
- cloud chat remains conversation-history authority;
- secrets/cookies/prompt bodies are not dumped to logs or committed.

## Orchestration plan

Use the smallest useful parallelism. Default maximum simultaneous implementation
lanes: 3. Do not create agents merely to increase count.

### Phase 1 — independent reconnaissance, parallel

Run these in parallel:

1. **DSH seam investigator — deep**
   - read current DSH package instructions and testing policy;
   - trace `SubagentProvider`, `SubagentRun`, cancellation, capability checks,
     REAL composition requirements, bundle/aggregate wiring, and current Stable
     Routing integration;
   - determine the exact canonical source locations to change;
   - report the minimal correct provider seam and any blocker.

2. **Browser Bridge contract investigator — balanced**
   - inspect current Bridge/Driver code and tests;
   - verify live `/health` and current status vocabulary;
   - identify any minimal API/process work needed for DSH cancellation,
     lifecycle, structured repair, or one-command operations;
   - do not send a live Gemini prompt during reconnaissance unless needed to
     resolve a material unknown.

3. **Workflow/routing investigator — deep**
   - inspect the current effective `@dsh-external/workflow` behavior and actual
     deployed Stable Routing/profile config;
   - prove how subagent providers are selected today and where Browser Chat can
     be added without introducing workflow-authored provider/model overrides;
   - identify which current workflows require `toolFilter`, `outputSchema`,
     persona, tools, or model telemetry.

Completion criterion for Phase 1: every current Consumer and required
capability is accounted for, and the investigators agree on or explicitly
surface the remaining seam decision. Do not start implementation while the run
identity or capability contract is still guessed.

### Phase 2 — architecture decision — deep

Synthesize Phase 1. Prefer a dedicated DSH `SubagentProvider` named according to
current repository conventions if the evidence supports it. The intended
identity is one `SubagentProvider.start()` per Browser Chat Bridge run, NOT one
top-level workflow run shared by parallel children.

If a different seam is required, state exactly why the preferred seam violates
a current DSH contract. Do not widen generic `GenerateOptions` or add a new
global abstraction unless a current Consumer forces it.

Freeze the provider/Bridge interface and acceptance matrix before code changes.

### Phase 3 — implementation, parallel where independent

After the interface is frozen, use up to two parallel implementation lanes:

1. **DSH provider implementer — balanced/deep**
   - implement the provider package/plugin, config, provider registration,
     lifecycle, error mapping, cancellation/dispose, capability declarations,
     tests, docs, aggregate/reference/bundle wiring required by current policy;
   - use TDD;
   - implement truthful structured-output support if the Phase 2 decision says
     it is required and correct;
   - keep diagnostics bounded and privacy-safe.

2. **Bridge operations implementer — balanced**
   - only if Phase 2 proves changes are required;
   - add the minimum cancellation/health/launcher/process behavior needed by the
     provider contract;
   - preserve Bridge/Driver separation and current send/idempotency safety;
   - do not refactor working Gemini DOM code without a requirement.

Do not have both lanes edit the same repository files concurrently. Merge/rebase
their work deliberately and run repository-local tests after integration.

### Phase 4 — integration verification — balanced

Run focused tests first, then the full required repository gates. Include DSH's
mandatory non-unit REAL composition test for a product-visible provider.

Then perform live E2E using the actual Browser Chat Bridge, Driver, authenticated
Chromium, and DSH:

1. health checks PASS;
2. one DSH Browser Chat-backed provider run completes with expected text;
3. a second provider run gets a different conversation;
4. duplicate/reconciled request behavior does not add another User turn;
5. if structured repair exists, one invalid first format is repaired with turn
   2 on the same conversation and never more than one repair;
6. abort/dispose behavior matches the documented contract;
7. run the smallest capability-compatible External Workflow smoke and prove the
   child reaches a legal terminal result through central routing/provider
   policy.

Never weaken preflight or claim capabilities just to make the workflow smoke
pass.

### Phase 5 — independent review — deep

Use a reviewer that did not author the implementation. Review BOTH changed
repositories plus effective deployment configuration for:

- correctness/lifecycle;
- run/thread isolation under concurrency;
- send idempotency and `AMBIGUOUS` behavior;
- cancellation/quiescence;
- DSH architecture and package policy;
- truthful capabilities;
- structured-output parser/validator and bounded repair;
- loopback/network security;
- secrets/log privacy;
- Stable Routing authority and absence of override backdoors;
- test adequacy and REAL composition evidence;
- operational process behavior;
- over-engineering/dead code.

Return findings with concrete file/line/evidence and severity. An approval with
unresolved blocking findings is invalid.

### Phase 6 — repair — balanced

Repair every blocking review finding. Add regression tests before or with each
behavioral fix. Re-run focused and affected full gates plus live checks whose
contract changed.

### Phase 7 — re-review and final judge — deep

Run an independent re-review after repairs, then a separate final judge. The
final judge must verify the requested acceptance criteria against code, tests,
Git state, live evidence, and process state rather than trusting prior summaries.

## Implementation requirements

Follow the detailed requirements in
`DSH_ORCHESTRATOR_HANDOFF_20260904.md`. In particular:

- use a provider-owned unique Bridge `run_id` per subagent start;
- use deterministic request IDs such as `<run>:turn:1` and at most
  `<run>:turn:2` for one structured repair;
- only `COMPLETED` becomes a completed subagent result;
- `AMBIGUOUS` never resends the same browser turn;
- unsupported modalities/capabilities reject before dispatch;
- do not replay full DSH history into a cloud conversation;
- do not add generic DSH tool calling in this task;
- do not make Obscura production-authoritative;
- do not modify unrelated DSH untracked files;
- do not hand-edit installed `tools/dsh-workflow-iw/lib` build output as source.

## Git and completion

Use dedicated worktrees/branches for code changes. Before finishing, inspect
`git status`, combined diffs, ignored runtime artifacts, and exact commits in
each repository. Commit task changes in their owning repositories without
unrelated files.

Leave production processes in a known state: the intended Chromium Bridge and
Driver healthy if the current deployment expects them running; test-only
Obscura/shadow/temporary servers stopped.

Report progress as a percentage. 100% means there is no remaining
AI-executable work requested here. The final report must include:

- architecture chosen and why;
- files/packages changed;
- exact commits;
- unit/focused/full/REAL-composition test results;
- live DSH and workflow E2E evidence including conversation identity behavior;
- review findings and repairs;
- effective configuration/operating commands;
- remaining limitations that truly require a future feature or human action.

Begin now from current-state discovery and continue through the final judge.
