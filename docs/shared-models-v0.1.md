# v0.1 shared models

The public interfaces are `argos.models` (persistent entities),
`argos.protocols` (component messages), and `argos.common` (shared values and
status enums). They use Pydantic v2 and Python 3.11+. No component runtime,
persistence, prompts, or execution engine is included.

## Validation boundary

An Orchestrator must validate untrusted agent JSON before applying it to state:

```python
from argos.protocols import ManagerAction

action = ManagerAction.model_validate_json(raw_agent_json)
serialized = action.model_dump_json()
restored = ManagerAction.model_validate_json(serialized)
assert restored == action
```

All models reject unknown fields, missing required fields, invalid enum values,
blank descriptive strings, malformed UUIDs, and timezone-naive timestamps.
Timestamps are ISO 8601 with an explicit offset; UUIDs are stable identifiers
allocated by the caller, never regenerated during deserialization. There are no
implicit timestamp defaults. JSON schemas are available via `model_json_schema()`.

Treat validated messages as snapshots. Revalidate serialized data at the state
boundary after any modification; Pydantic validation is not an authorization
mechanism and in-place list edits are not assignment-validated. Do not use
`model_construct()` or unchecked `model_copy(update=...)` for agent input.

## Entity relationships

| Entity | Required provenance / purpose |
| --- | --- |
| Project | `id`, name, human-defined question and context in `config` |
| Subproblem | `project_id`; a question managed by RM |
| ResearchBranch | `subproblem_id`; named grouping of related work |
| Hypothesis | `subproblem_id`, optional `branch_id`; testable statement |
| Experiment | `spec.hypothesis_id`; `spec.experiment_id` must equal its own `id`; optional `branch_id` |
| Run | `experiment_id`; execution attempt and optional evaluator measurements |
| Observation | `run_id`, objective `evaluation`; nested run reference must match |
| Claim | `project_id`, statement, supporting and contradicting observation IDs |
| Decision | `project_id`, RM or human actor, rationale, typed entity references, optional requested action |

The provenance chain is Project → Subproblem → Hypothesis → Experiment → Run
→ Observation → Claim. A hypothesis or experiment joins a branch via its
`branch_id`; the branch must belong to the hypothesis's subproblem. Reverse
relationships are derived from references instead of duplicated lists.

Claims require at least one supporting or contradicting observation, with no
duplicates or overlap. A purely contradicted claim is valid. An idea without
observations remains a hypothesis, not an evidence-backed claim. Decisions
preserve significant choices and may reference any entity through
`EntityReference(entity_type, entity_id)`.

Local validators check nested IDs, execution outcomes, and evidence consistency.
The future state layer must check referenced records exist, belong to the same
project/subproblem, and have matching authoritative content. It must also enforce
unique IDs, legal status transitions, run/evaluation/observation consistency, and
human authorization. These cross-record operations are deliberately not a
persistence implementation in this issue.

## Status semantics

- `EntityStatus`: active, completed, paused, rejected, archived. Claims start
  active as candidates; Critic verdicts are separate records and do not mutate
  their status automatically.
- `ExperimentStatus`: proposed, selected, running, completed, cancelled.
  Completed means the experiment's work is finished, not that a hypothesis held.
- `RunStatus`: pending, running, succeeded, failed. Each execution attempt has
  its own ID. Only terminal runs contain an `ExperimentResult`.
- `CriticVerdict`: accept, reject, needs_more_evidence.

A terminal Run retains source/resulting commits, configuration, commands,
stdout/stderr, artifact references, and failure details in `result`. Successful
runs may attach authoritative metrics in `evaluation` once evaluation finishes.
Pending/running runs do not yet have a terminal result. A failed result may lack
a resulting commit or commands (for example, command validation failed before
execution). Successful results require a resulting commit and zero-exit command
records. A successful no-change run may use the source commit as its resulting
commit. These strings identify commits but are not verified against a repository.

## Component messages

`ProjectConfig` supplies research context, canonical question, source repository,
build/test commands, evaluation protocol, path scope, and resource limits.
`ResearchTask` gives an RA a specific question, perspective, bounded context,
and relevant evidence references. The caller must select context and exclude
other RAs' initial outputs. `ResearchAgentResult` links to its task and includes
ideas, hypotheses, rationale, expected effects, validation methods, risks, and
assumptions. Empty ideas are allowed when exploration produces no proposal.

`ManagerAction.action` is a discriminated union keyed by `action_type`. Each
variant has a specific payload, not an arbitrary dictionary:

| Action | Payload |
| --- | --- |
| create_subproblem | question |
| update_subproblem | subproblem ID, replacement question |
| dispatch_research_agents | one or more bounded ResearchTasks |
| create_hypothesis | subproblem ID, statement, rationale, optional branch ID |
| select_hypothesis | hypothesis ID |
| propose_experiment | ExperimentSpec |
| form_claim | statement, evidence references |
| revise_claim | claim ID, replacement statement and evidence references |
| request_critic_review | claim ID |
| continue_research | next question |
| pause_for_human | question for the human |
| propose_main_question_revision | proposed question; requires_human_approval is always true |

There is **no action that directly changes the canonical main question**.
A revision action is only a proposal. The future Orchestrator/state layer must
obtain explicit human approval through a trusted human interface before applying
it, and record the human decision. An agent-supplied actor label or approval flag
is not proof of approval. ProjectConfig describes data; constructing a replacement
config does not authorize its persistence. RM rationale is for recording decisions;
downstream ExperimentSpec and evaluator messages require no RM private reasoning.

`ExperimentSpec` includes the hypothesis ID and statement, goal, requested change,
build/test/run steps, evaluation protocol, limits, and optional path scope. Commands
are argument arrays, not instructions to an execution engine. Empty build/test
step lists explicitly mean no such step is needed; run steps must be nonempty.
Path scope describes repository-relative paths/globs. A future executor must
resolve and enforce scope and resource limits, including inherited ProjectConfig
scope when a spec omits it. These schemas neither execute commands nor grant
filesystem access.

`ExperimentResult` represents execution only. Exactly failed results require
`ExecutionFailure` (build failure, test failure, runtime crash, timeout, invalid
command, or invalid evaluator output). A measurement failing a threshold is not
an execution failure. A failed execution must not become scientific evidence
falsifying a hypothesis.

`EvaluatorResult` contains finite numerical measurements, numerical constraint
checks, protocol identity, and artifact references. It has no interpretation,
hypothesis verdict, or research-direction field. Names must be unique in each
measurement/check list. Text labels cannot be semantically policed by a schema;
only deterministic evaluator output should populate this message. Invalid
evaluator output is recorded as an execution failure, not fabricated metrics.

`CriticReview` links a claim to a verdict, concise rationale, weaknesses, risks,
and requested checks. An empty check list is allowed when no further check is
requested. Review does not itself apply a decision.

## Executable examples

[The entity fixture](../examples/entities.json) includes all nine entities linked
through one provenance chain. [Protocol examples](../examples/protocols/) include
ProjectConfig, every ManagerAction variant, ResearchTask, ResearchAgentResult,
ExperimentSpec, successful and failed ExperimentResult, EvaluatorResult, and all
three Critic verdicts. They are illustrative payloads, not executed experiments.

The successful example measures 12 ms against a 10 ms limit: execution succeeds,
the constraint fails, and the claim is contradicted. The failed example records
a build failure without measurements. Tests load all checked-in examples and
round-trip every nested model through JSON, alongside invalid-input cases.

Both developers' review and approval of these interfaces remain required before
merge. This implementation does not claim that human review has occurred.
