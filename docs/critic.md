# Independent Critic (issue #7)

`argos.research.Critic` defines `review(CriticInput, task_id=...)`, returning an
`AgentOutcome[CriticReview]`. `LLMCritic` uses the same stateless `LLMBackend` and
bounded `StructuredCaller` as RM/RA. `FakeCritic` accepts responses keyed by task
UUID, including validated reviews, invalid JSON and exceptions; it exercises the
same validation path without making API calls.

`CriticState.prepare` builds a claim-specific input from SQLite. It includes the
approved main question, held-out protocol, exact claim wording, every supporting
and contradicting observation, relevant subproblem question, ExperimentSpec
(including tested hypothesis and evaluation protocol), raw ExperimentResult,
EvaluatorResult and code diff. It excludes decisions, hypothesis rationale,
research-branch history, RM synthesis and other reviews. Claim scope is expressed
in the existing claim statement. Unknown input fields are rejected.

Diff contents are supplied by trusted host code, keyed by run UUID. Missing diffs
fail explicitly; an empty diff is valid. The Critic never opens artifact paths or
executes commands. Raw results carry command output and provenance references;
the host must retain the referenced artifacts. Baseline comparisons are passed
through from the recorded evaluator result; missing baseline or replication
information remains a reason to request evidence, never an invented measurement.

The prompt asks the Critic to search for counterevidence, confounders, hidden
regressions, unfair baselines, incorrect metrics, missing ablations, alternative
explanations and overgeneralization. It also allows acceptance when evidence is
sufficient for the exact scope. All verdicts require a nonempty rationale and
explicit weaknesses, risks and requested-check lists. `needs_more_evidence` now
requires at least one nonempty requested check. Checks retain the shared protocol's
structured JSON array of actionable strings; no new planning protocol is added.

## Host integration

```python
from uuid import uuid4
from argos.research import CriticState, LLMCritic, ResearchStateWriter, StructuredCaller

writer = ResearchStateWriter(store)
caller = StructuredCaller(backend, record_task=writer.record_task, llm_slots=4)
# Share caller across roles when using the same resource pool.
critic = LLMCritic(caller)
reviews = CriticState(store)
context = reviews.prepare(claim_id, code_diffs=trusted_diffs_by_run_id)
outcome = await critic.review(context, task_id=uuid4())
review_id = reviews.record(context, outcome)
if review_id is not None:
    next_plan = await manager.plan(
        store.snapshot(context.project_id),
        task_id=uuid4(),
        event="review_recorded",
    )
```

Wire `record_task` for durable execution. Malformed output, wrong claim identity,
blank rationale and unactionable evidence requests receive at most one repair.
A second invalid output, backend error, timeout or context overflow produces a
failed/timeout Task and no review. The shared caller enforces finite concurrency,
a total deadline and default 60,000-character input/output limits. Oversized input
is rejected, not silently stripped of counterevidence. Repairs only contain this
review's original context, invalid response and validation error.

`CriticState.record` revalidates the outcome and project/claim/task identity, then
checks the prepared claim and evidence against current state inside a transaction.
Stale reviews are rejected. The existing `StateStore.record_review` saves the
validated review and exact reviewed claim version. Reviews survive reopening
SQLite and enter the bounded RM snapshot, including structured requested checks.
RM's prompt treats `needs_more_evidence` as unresolved and uses those checks when
proposing the next research action. Metrics, hypothesis status and claim status
are not changed by the Critic. An `accept` review applies only to its saved claim
version; it does not mark later revisions accepted.

## Scope and limitations

This implements the review component and host integration helpers, not issue #8's
orchestrator, event delivery, dispatch, restart recovery or CLI. The host emits the
completed event only after saving the review, handles persistence errors, and must
archive pending inputs/outcomes if it needs crash recovery between model completion
and review insertion. Review insertion is append-only, not event-deduplicated.

Provider adapters must honor fresh conversations. Schema projection prevents
passing an RM transcript field, but cannot detect private reasoning deliberately
copied into allowed evidence prose. Artifact authenticity and loading are trusted
host responsibilities; supplied diff contents are not verified against disk by
this component. Scientific review quality is a model concern: scripted verdicts
prove data flow and validation, not autonomous scientific judgment.

No real model call, synthetic autonomous E2E, real research repository validation,
≥8-hour run, human interface approval or scientific acceptance is claimed here.
Human review remains pending for the PR. No FGIM code, data, memory or results were
used. The full architecture, task1.md and project-policy.md were supplied in the
user task; separate files are absent in this checkout. Repository guidance and
issues #1–#8 were read, with the latest user requirements taking precedence.

## Handoff and validation

Project/repository: ARGOS / `ykindred/argos-research`.
Branch: `codex/issue-7-20260917184407`.
Task: issue #7, Critic. Evidence: `tests/test_critic.py`,
`src/argos/research/critic.py`, role prompts and this document.

Self-review checked role boundaries, complete evidence projection, claim identity,
stale input rejection, failure semantics, durable reviews and the RM feedback path.
The first focused test run found two tests incorrectly attempting to mutate
append-only observations/decisions; those tests were corrected to respect the
store contract. Development uses uv with Python 3.12.3 and a cache under `/tmp`.

Final validation:

- `UV_CACHE_DIR=/tmp/argos-issue7-uv-cache uv run pytest -q`:
  **385 passed in 20.77 s**, including 18 Critic tests.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: passed (30 files).
- `uv build`: source distribution and wheel built successfully.
- `git diff --check`: passed.

All uv commands used the same `/tmp` cache. No commits, pushes, PRs, issue comments,
workflow changes or human approvals were made.
