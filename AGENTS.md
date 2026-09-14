<!-- iw-spec-kit-policy:start -->
## Spec Kit development authority

- This repository follows the formal Intelligence Works Spec Kit baseline. Small changes that do not introduce a new behavior contract may use direct Engineering Skills; behavior/interface/acceptance-criteria/design changes use Spec Kit Agentic SDD; DSH External Workflow is added only for durable/parallel/resumable execution.
- Authority is split deliberately: `AGENTS.md` = operational policy; `.specify/memory/constitution.md` = repository development principles; `spec.md` = requirements/AC; `plan.md` = technical design; `tasks.md` = development state; DSH/External Workflow = execution state; Code/Tests/verification = implemented and verified facts.
- External Workflow may execute/retry/resume/recover Spec Kit tasks and return evidence, but it must not silently change requirements, weaken acceptance criteria, replace plan decisions, or create a competing canonical task hierarchy.
- Memory is supplemental context only; do not mirror current repository-authoritative Spec Kit/Git/code/test state into Memory.
- Shared version pin, rollout policy, and detailed rationale are owned by `tools/dsh-workflow-dispatch/docs/SPECKIT_FORMAL_ADOPTION_20260914.md` from the Intelligence Works root.
<!-- iw-spec-kit-policy:end -->
