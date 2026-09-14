<!-- iw-spec-kit-constitution: v1 -->
# Intelligence Works Development Constitution

## Core Principles

### I. Explicit Development Authority
Feature requirements and acceptance criteria live in `spec.md`; technical design lives in `plan.md`; development work and remaining work live in `tasks.md`. Code, tests, and verification evidence remain the authority for what is actually implemented and proven. A completed execution run never overrides unmet acceptance criteria or failing verification.

### II. Minimal Correct Change
Prefer the smallest change that satisfies the accepted requirement and preserves existing contracts. Reuse existing code and platform capabilities before adding abstractions, dependencies, configuration layers, or speculative extension points. Structural complexity must have a current, evidenced need.

### III. Verification Is Part of Completion
Behavior changes require the smallest meaningful regression evidence available in the repository. Existing test, lint, typecheck, build, static-validation, and runtime-smoke gates that apply to the changed surface must pass before completion is claimed. A task marked complete without required verification is not complete.

### IV. Authority Changes Are Deliberate
Implementation may discover that a requirement or design must change, but execution must not silently rewrite the contract. When implementation conflicts with `spec.md` or `plan.md`, return to the appropriate Spec Kit artifact, update the authority explicitly, regenerate/reconcile tasks as needed, then resume implementation.

### V. Security and Evidence Boundaries Stay Intact
Do not weaken authentication, authorization, privacy, secret handling, trust-boundary validation, safety constraints, or repository-specific evidence requirements to make implementation easier. Security-sensitive or irreversible behavior requires explicit verification appropriate to the repository.

## Development Process

- Small changes that do not introduce a new behavior contract or acceptance criterion may use direct engineering Skills without creating a feature spec.
- Medium changes that introduce behavior, interfaces, acceptance criteria, persistence/schema changes, security boundaries, or architecture decisions use Spec Kit Agentic SDD.
- Large changes are Medium changes that additionally need durable resume/recovery, multi-agent execution, parallel lanes, long-running execution, or multiple independent verification gates. Only those execution concerns may use DSH External Workflow.
- `clarify`, `checklist`, and `analyze` are quality gates used when the change benefits from them; they are not ceremony required for every feature.
- `converge` reconciles implementation and evidence against the accepted artifacts and appends remaining work instead of silently declaring success.

## DSH / Workflow Boundary

DSH is the Agent Runtime and owns model/provider selection, reasoning effort, capacity, fallback, subagent execution, retry, resume, and recovery. DSH External Workflow may read Spec Kit tasks, execute them, produce evidence, and report execution state. It must not create a competing requirement/design/task authority, weaken acceptance criteria, or silently replace `spec.md`, `plan.md`, or `tasks.md`.

## Governance

`AGENTS.md` owns operational rules such as tools, Git, Workbridge, Skill routing, and execution policy. This constitution owns repository development and quality principles. Feature artifacts own feature-specific truth. Memory is supplemental context only and must not duplicate current repository-authoritative state already recorded in Spec Kit/Git/code/tests.

Baseline changes are versioned in `tools/dsh-workflow-dispatch/spec-kit-baseline.json` and rolled out through the shared bootstrap/verify scripts. Repository-specific amendments may strengthen this constitution but must preserve the authority hierarchy above unless the user explicitly changes the platform policy.

**Version**: 1.0.0 | **Ratified**: 2026-09-14 | **Last Amended**: 2026-09-14
