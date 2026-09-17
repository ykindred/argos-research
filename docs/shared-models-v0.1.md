# v0.1 shared models

The public interfaces are `argos.models` (persistent entities), `argos.protocols`
(component messages), and `argos.common` (shared values/enums). Python 3.11+ and
Pydantic v2 are required. This issue defines contracts only: no SQLite store,
agent prompts/backends, project loader, execution engine, scheduler, or loop.
The latest user-provided full architecture, task1.md, and project policy take
precedence over older architecture/issue text. ARGOS and the `argos` package keep
their names; the old two-developer implementation restriction does not apply.

## Validation boundary

```python
from argos.protocols import ManagerAction

action = ManagerAction.model_validate_json(raw_agent_json)
assert ManagerAction.model_validate_json(action.model_dump_json()) == action
```

All models reject unexpected fields and missing required fields. Enums, UUID
identifiers, nonblank descriptive strings, finite measurements, positive integer
resource limits, and timezone-aware timestamps are validated. Caller-assigned
IDs and timestamps survive JSON round trips. `model_json_schema()` exposes the
contract. Models remain domain-neutral.

These are validated snapshots, not authorization objects. Revalidate serialized
messages at the state boundary after modification: in-place list mutations,
`model_construct()` and `model_copy(update=...)` bypass validation. Never use
those bypasses to accept agent input. Agent labels such as `actor="human"` and
approval decision IDs do not prove human authorization.

## Persistent entities and provenance

| Entity | Relationship and purpose |
| --- | --- |
| Project | canonical ProjectConfig; optional current baseline ID |
| Subproblem | project ID, optional parent subproblem, priority, update timestamp |
| ResearchBranch | subproblem ID; objective in description; independent/shared/blind context policy |
| Hypothesis | subproblem and optional branch; statement, rationale, confidence, update timestamp |
| Task | project, kind, resource class, input/output entity references, durable status, timing, failure, repair count |
| Experiment | hypothesis through spec; optional research branch; full lifecycle status |
| Run | experiment ID; execution attempt, terminal result and evaluation |
| Observation | run ID and evaluator output; RM summary, relation, confidence |
| Claim | project ID, statement, explicit scope, supporting/contradicting observation references |
| Evidence | claim → observation → run, with relation |
| Decision | project, cycle, decision type, RM/human actor, rationale and affected entity references |
| Baseline | project, source commit, run/evaluation, approval decision, previous baseline ID |

The primary chain remains Project → Subproblem → Hypothesis → Experiment → Run
→ Observation → Claim. Research branches are scientific routes, not Git branches.
Task records persist dispatch/failure independently of a particular agent session;
ResearchTask is the bounded RA request, linked by its ID to a Task of kind
`explore`. Evidence records make the claim-to-run link explicit. Claim evidence
lists are retained as a convenient projection; the store must keep them consistent
with Evidence records. Claims require at least one observation; contradicted-only
claims are valid. Unmeasured ideas remain hypotheses.

The store must check reference existence, same-project/subproblem membership,
branch membership, unique IDs, parent cycles, authoritative nested contents,
legal transitions and matching Task/result identities. An Evidence run must equal
its Observation run. Invalid observations cannot support or contradict claims.
These cross-record guarantees are not implemented by local model validation.

Baseline records are append-only historical snapshots. Initial creation requires
an explicit human initialization request; refresh requires explicit human approval,
a new baseline ID and a link to the prior record. Project.baseline_id selects the
current snapshot; ExperimentSpec/EvaluatorResult.baseline_id pin comparisons.
The runtime must resolve omitted spec baseline IDs to the current baseline before
execution and preserve that resolved ID. Clean-checkout verification, human
approval, and immutability must be enforced by the loader/executor/store. A model
alone neither proves a checkout clean nor authorizes replacing a baseline.

## Status and failure semantics

- SubproblemStatus: open, active, blocked, resolved, rejected.
- HypothesisStatus: proposed, testing, supported, contradicted, inconclusive, rejected.
- ContextPolicy: independent, shared, blind.
- ExperimentStatus: planned → implementing → implemented → testing → running →
  evaluating → completed; failure states implementation_failed, test_failed,
  run_failed, invalid_result, timeout. Legacy proposed/selected and cancelled are
  retained for draft interface compatibility; new experiments default to planned.
- RunStatus: pending, running, succeeded, failed. This summarizes an attempt;
  ExperimentStatus provides phase detail, ExecutionFailure provides failure kind.
- TaskStatus: pending, running, completed, failed, timeout, cancelled. Terminal
  tasks require finish timestamps; failed/timeout tasks require failure details.
  Only completed tasks may publish validated output references.
- EntityStatus remains the generic project/branch/claim vocabulary. A Critic
  verdict does not automatically change a claim's status.

ExperimentResult describes execution, not scientific value. Failed execution
requires structured failure details; successful execution requires a resulting
commit and recorded zero-exit commands. Successful no-change runs may use the
source commit as the resulting commit. Pre-execution failures may have no commands
or resulting commit. Worktree, diff, log paths, configuration, commands, artifacts,
and timestamps preserve provenance. Artifact paths are references, not proof that
files exist. Executors must retain even empty diff/log files where appropriate.

A build failure maps to experiment implementation_failed; test failures to
test_failed; runtime crashes/invalid commands to run_failed; malformed evaluator
output to invalid_result; timeout to timeout. Failures never automatically falsify
a hypothesis. A valid measurement worse than baseline remains a successful
execution and can subsequently be interpreted by RM as contradictory evidence.

EvaluatorResult reports validity (`ok`/`invalid`), objective finite measurements,
metric direction (minimize/maximize/informational), constraint checks and artifacts.
Boolean correctness checks need no numerical threshold. A failed constraint does
not necessarily mean invalid evaluation: the evaluator decides validity under the
human-approved protocol. An `ok` result requires measurements. An invalid result
may retain partial measurements but can only produce an invalid Observation;
it cannot support scientific claims. Malformed JSON produces an execution failure,
not fabricated measurements. Failed Runs cannot attach evaluation; a well-formed
invalid evaluation can be retained on a successfully executed Run and invalid
Observation while the Experiment becomes invalid_result.

RM writes Observation summary/relation/confidence; evaluator messages contain no
hypothesis verdict, novelty or scientific interpretation. Text semantics cannot
be policed by a schema: consumers must accept measurements only from deterministic
evaluation and verify them against the approved protocol.

## Component contracts

ProjectConfig carries Research Charter text, human-owned direction/question,
held-out protocol, repository, commands, evaluation protocol, editable/protected
scope, task resource limits, separate LLM/coding/CPU/GPU slot counts, cycle budget
and stagnation threshold. It is the normalized loader output, not a YAML parser.
ProjectLoader will read research.md + project.yaml + project evaluator, resolve
paths and enforce permissions. The full architecture's nested YAML shape can be
adapted into this existing flat contract without putting project logic in core.

ResearchTask includes branch ID, context policy, bounded context, question,
perspective and evidence references. Dispatch accepts one or more tasks, including
at least three independent branches; task IDs and independent branch IDs must be
unique, and task projects must match the ManagerAction project. The example uses
three perspectives including falsification. The orchestrator must actually isolate
initial contexts and join completed results before RM synthesis. A policy enum
cannot detect another RA's output embedded in free text.

ResearchAgentResult contains ideas, hypotheses, rationale, expected effects,
validation methods, risks and assumptions. Empty ideas are allowed. ExperimentSpec
contains prediction, success/failure criteria, requested change, hypothesis, goal,
build/test/run steps, evaluation protocol, resource class/limits, baseline and scope.
Empty build/test step lists mean no such step is needed. Run steps are nonempty
argument arrays. The executor must resolve inherited scope and prevent a spec from
relaxing protected project policy or replacing the approved evaluator.

CodingTask is implementation-only: task/experiment IDs, requested change and scope.
CodingResult reports implemented/failed/timeout with diff/log references. It is
not ExperimentResult and cannot assert successful scientific execution. EA exits
after implementation or necessary repair. Deterministic code subsequently performs
build/test/run/evaluate; a failure needs an explicit decision before invoking EA
again. Backend implementations and their common role interfaces/prompts belong to
the component issues, not this schema-only change.

CriticTask is the typed blind-review input: task ID, main question, claim ID,
statement, explicit scope and selected ReviewEvidence records. Each record contains
an observation ID, ExperimentSpec, execution provenance (including diff/log paths)
and raw EvaluatorResult. Local validation checks matching experiment/run IDs,
protocol name and baseline ID, successful execution, and unique observation IDs.
Negative measurements and well-formed invalid evaluations remain reviewable so the
Critic can challenge insufficient evidence. Failed execution is recorded as failure,
not as scientific evidence. No RM summary or private reasoning field is required.

The caller must select a finite evidence/context budget, authenticate authoritative
records and resolve diff/artifact references before invoking Critic. Matching names
and IDs alone do not prove evaluator integrity. The store must verify the claim's
scope and statement, observation membership, and that selected evidence includes
relevant counterevidence rather than cherry-picked support. Free text and artifact
contents still require context isolation; a schema cannot prove blindness.
CriticTask.task_id refers to the durable review Task; the caller must correlate the
returned CriticReview to that invocation and claim.

Claim and form_claim/revise_claim now require explicit nonblank scope. Existing
serialized draft payloads must be supplied with their intended scope by the caller;
the model does not infer scientific applicability from the statement.

CriticReview carries accept/reject/needs_more_evidence, mandatory concise rationale,
weaknesses, risks and requested checks. Critic must receive only the relevant claim,
experiment, diff, raw evidence and protocol, without RM private reasoning. It seeks
counterevidence but may accept sufficiently supported claims. No downstream message
requires private reasoning.

## Manager actions

ManagerAction.action is a discriminated union keyed by action_type. Existing
lowercase wire names are preserved:

| Action | Payload / full architecture equivalent |
| --- | --- |
| create_subproblem / update_subproblem | question / existing ID and question |
| close_subproblem | subproblem ID |
| dispatch_research_agents | bounded ResearchTasks (EXPLORE) |
| create_hypothesis / select_hypothesis | statement/rationale/branch or hypothesis ID |
| propose_experiment | ExperimentSpec (DESIGN_EXPERIMENT) |
| implement_experiment / run_experiment | experiment ID |
| form_claim / revise_claim | statement, scope, evidence; existing ID for revision |
| request_critic_review | claim ID (REVIEW) |
| synthesize | completed RA task IDs; RM performs synthesis |
| continue_research | next question |
| pause_for_human | question and optional options (ASK_HUMAN); rationale in envelope |
| stop | reason |
| propose_main_question_revision | proposed question, approval always required |
| propose_protected_change | direction/question/evaluator/baseline/held-out target and proposal; approval always required |

No action directly edits protected canonical settings. Human approval must come
from a trusted human interface, be tied to the exact proposed change, and be
recorded as a Decision before applying it. ProjectConfig replacement or agent
approval flags cannot bypass this gate. Generic human questions, stagnation gates
and implementation retries also remain explicit decisions. Merge/interface review
by humans remains pending; no automatic approval or old two-person staffing
requirement is implied by this revision.

## Remaining cross-component guarantees

- Validate structured output, repair at most once, then persist Task failure without
  publishing invalid state. `repair_attempts` is bounded to one; models do not
  implement retries or rollbacks.
- Trigger RM from completed research events with a finite briefing, including
  failed directions; do not poll RM or replay all history. Preserve synchronous
  cycles with logical independent branches, without an asynchronous Idea Bank.
- Enforce finite asyncio.Semaphore pools separately for LLM, coding, CPU and GPU.
  ResourceSlots config alone does not schedule work.
- Persist transitions before advancing; one task failure must not kill the loop.
  Implement process-tree termination, pause/resume, restart recovery and stagnation
  human gates in execution/orchestration issues.
- Preserve failures, diff, logs and provenance before cleaning failed worktrees.
  Retain evidence outside deleted worktrees; never delete the only evidence copy.
- Later issues supply CLI, SQLite, fake backends, synthetic E2E and then one real
  backend. No E2E, actual isolation, recovery, real-repository run or ≥8h run is
  claimed by protocol unit tests.

## Examples and validation

[Entity fixtures](../examples/entities.json) and [protocol examples](../examples/protocols/)
cover the provenance chain, Task/Evidence/Baseline and baseline human Decision,
all ManagerAction variants, three independent RA branches, CodingTask/Result,
ExperimentSpec, successful and failed execution, evaluator measurements and all
three Critic verdicts plus a blind CriticTask request. They are illustrative payloads, not real experiment evidence.
The negative example measures 12 ms against a 10 ms threshold: execution succeeds,
the constraint fails, and RM records contradiction.

Run `uv run pytest`, `uv run ruff check src tests` and
`uv run ruff format --check src tests`. Tests round-trip examples and nested models,
reject invalid inputs and exercise provenance, task failure, lifecycle vocabulary,
human proposal boundaries, resources and execution/scientific-result separation.
Human review of the shared interfaces remains pending for the PR.

### Validation performed for this revision

Self-review checked this revision against issue #2 and the supplied updated
architecture, task1.md and project-policy requirements (those three source files
are supplied in the task, not present in this checkout). Existing Task, Evidence,
Baseline, lifecycle, resource and human-proposal contracts were preserved. This
follow-up adds explicit claim scope and typed blind-review inputs, with identity
and provenance validation and negative/invalid evidence tests.

Validation with uv on Python 3.12.3:

- `uv run pytest -q`: **239 passed** (1.15 s).
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: passed (5 files).
- `git diff --check`: passed.

Commands used `UV_CACHE_DIR=/tmp/argos-uv-cache` to keep cache writes within the
permitted filesystem. The tests validate contracts, not human authorization,
actual clean worktrees, physical RA isolation, SQLite persistence, scheduling or
process termination. Human interface review remains pending. No real backend,
real research repository, synthetic runtime E2E or eight-hour runtime test was run
in this protocol issue.

Handoff: ARGOS repository, branch `codex/issue-2-20260917162832`, issue #2 shared
protocol revision. Evidence is in `tests/test_models.py`, `examples/entities.json`,
`examples/protocols/` and this validation record. No FGIM code or experiment results
were used as ARGOS validation evidence.
