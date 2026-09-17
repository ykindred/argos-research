# Research Manager and independent Research Agents (issue #4)

The research layer provides a provider-neutral RM → independent RA → RM synthesis
flow. It does not execute experiments or implement the complete autonomous loop.
The latest user-supplied full architecture, task1.md and project-policy.md take
precedence over older role divisions. Those sources were supplied in the task;
separate files are absent from this checkout. README, architecture, development
principles, shared contracts, state documentation and issues #1–#8 were read.

## Interfaces and host integration

- `LLMBackend.complete(LLMRequest) -> str` is an async, stateless JSON interface.
  Every call carries a role prompt, JSON context and Pydantic output schema.
  A provider adapter must start a fresh conversation per request. No agent tools,
  SQL, shell access, credentials or provider-specific dependencies are supplied.
- `FakeLLMBackend` accepts scripted responses keyed by task UUID. It exposes copied
  requests for inspection, supports malformed responses and backend exceptions,
  and does not depend on concurrent call order.
- `ResearchManager.plan(snapshot, task_id=..., event=...)` returns an
  `AgentOutcome[ManagerPlan]`, containing shared `ManagerAction` objects.
  Events are initialization, recorded observations/review/hypotheses, or a human
  answer. `synthesize(snapshot, batch, task_id=...)` handles a joined RA round.
  These methods do not poll or write scientific state.
- `ResearchAgent.explore(ResearchTask)` returns an
  `AgentOutcome[ResearchAgentResult]`. Use perspective `falsification`
  (case-insensitive) for explicit weakest-assumption, counterevidence,
  alternative-explanation and cheap decisive-test instructions.
- `ResearchDispatcher.dispatch(ManagerAction)` freezes all independent task
  contexts before scheduling them and joins all outcomes before returning a
  `ResearchBatch`. Three or more independent branches are supported. Each RA gets
  only its assigned task and stable `ra:<branch UUID>` identity, never the snapshot,
  sibling task or sibling result. All successful results, including empty idea
  lists, and all failed task records reach RM synthesis without result truncation.

Share a `StructuredCaller` across RM and RA to share a finite LLM semaphore:

```python
from argos.research import (
    ResearchAgent, ResearchDispatcher, ResearchManager,
    ResearchStateWriter, StructuredCaller,
)

# store and backend are supplied by trusted host code.
writer = ResearchStateWriter(store)
caller = StructuredCaller(
    backend,
    llm_slots=project.config.resources.llm_slots,
    timeout_seconds=60,
    record_task=writer.record_task,
)
manager = ResearchManager(caller)
dispatcher = ResearchDispatcher(ResearchAgent(caller))

# dispatch_action is a validated RM dispatch action. Apply it before dispatch to
# persist branch identities and the RM decision; then join before synthesis.
writer.apply(dispatch_action, cycle=1)
batch = await dispatcher.dispatch(dispatch_action)
outcome = await manager.synthesize(
    store.snapshot(project.id), batch, task_id=fresh_manager_task_id,
)
# Host handles outcome.task failure or routes each validated output action.
```

Backend output is validated as an entire plan or result, then checked for project,
canonical question, RA identity and protected experiment settings. It gets at most
one repair, including semantic validation failures. The repair contains only that
call's invalid response and bounded validation error. The host records the repair
counter **before** the second call. A second invalid response publishes no output.
Backend errors and deadlines produce failed/timeout Tasks and do not cancel sibling
research tasks. The timeout includes semaphore queue time and both attempts.
Cancellation is recorded and re-raised; persistence failures propagate rather than
pretend that a task was saved. Deadlines rely on async backends honoring cancellation;
synchronous blocking provider code is not supported by this async contract.

Always wire `record_task` for durable use. Without it, outcomes are returned in
memory for component tests. With it, the existing SQLite Task lifecycle stores
start, repair and terminal records. Task IDs must be new per invocation; this API
never automatically resumes/retries a previously started task.

## State application and human authority

`ResearchStateWriter` is a trusted host adapter, separate from the agent classes.
It applies one action and its Decision in a transaction. It supports subproblem
creation/updates/closure, independent branch creation for RA dispatch, hypothesis
creation/selection, experiment proposal, candidate claim creation/revision, and
main-question revision proposals. Returned entities provide stable generated IDs
for the next RM call. Decisions link those entities; selection records intent and
does not mark a hypothesis supported or an experiment running.

Other actions (execution, Critic dispatch, stop/continue, generic human gates,
protected changes other than the main question) remain explicit host routing work
and raise `NotImplementedError` in this adapter. No action executes shell commands.
Use `store.transaction()` around multiple `apply` calls when the host needs an
atomic group; one action's transaction does not imply that a whole cycle committed.
The existing store rechecks entity membership, hypothesis statements, evaluator
identity and authoritative observations. Proposal scope must equal project scope;
resource ceilings cannot be loosened and the current baseline must be pinned.
This is a conservative v0.1 policy, not a general path-glob subset solver.

A main-question proposal persists its wording and pauses the project, leaving the
canonical question unchanged. Both RM calls and state application reject paused
projects. Only trusted human-interface code may call:

```python
writer.answer_main_question(
    project.id,
    proposed_question="Exact pending wording",
    approve=True,  # or False to reject and keep the canonical question
    rationale="Reason supplied by the human",
    cycle=2,
)
```

This requires exact pending wording, records the human decision, clears the
proposal and returns the project to active state. A stale or repeated answer
fails. An LLM-generated flag is never routed to this method. Initial question
approval uses the existing trusted `StateStore.approve_main_question` API. Human
authentication and interactive CLI input are host responsibilities.

## Context and scientific boundaries

RM gets the approved project configuration/charter, up to eight rows per relevant
frontier section, five recent decision summaries/references, compact experiment
and execution-failure records, and truncation indicators. It does not receive old
agent actions or full command stdout/stderr. Default input/output caps are 60,000
characters each; oversized context fails before calling the model, rather than
silently cutting the charter or synthesis evidence. These are character bounds,
not exact token estimates. The full budget-accounting BriefingBuilder belongs to
the orchestrator issue.

The RM prompt directs RM itself to combine duplicate ideas, preserve disagreements
and counterevidence, select hypotheses, propose cheap pilots, interpret measured
observations, and request Critic review. Synthesis quality remains a model concern;
the deterministic tests prove data flow and validation, not scientific judgment.
Metrics remain in evaluator-owned records. An RA/backend failure creates no
Observation and never changes a hypothesis to contradicted.

Shared protocols gained backward-compatible subproblem priority fields and optional
`ResearchIdea.falsification_suggestions`. Existing payloads remain valid.

## Validation and limitations

Tests in `tests/test_research.py` cover the fake RM → three isolated RAs → RM flow,
hypothesis promotion, selection, ExperimentSpec persistence, task histories and
reopening SQLite, identity/protected-field validation, one repair, sibling failure
isolation, all-failed and empty-idea cases, semaphore limits, timeout/cancellation,
persistence errors, bounded briefings, evidence-backed claims, and accept/reject/
stale human-gate answers. The existing model and state regression tests also run.

The full orchestrator must persist returned `ResearchBatch`/`AgentOutcome` payloads
before scheduling later events if it needs to resume synthesis after a crash.
This issue persists Task status and applied decisions/entities, not raw RA result
archives, event deduplication, or crash recovery. The host remains responsible for
supplying appropriate task context and for provider adapters honoring statelessness;
there is no semantic filter that can identify sibling prose copied into task text.

No real LLM integration, CodingBackend, evaluator, Critic agent, process execution,
synthetic executable project, complete autonomous E2E, real research repository,
or ≥8-hour run is claimed. Human review of interfaces remains pending for the PR.
FGIM code, memory, data and evidence were not used or modified.

## Handoff and actual validation

Project/repository: ARGOS / `ykindred/argos-research`.
Branch: `codex/issue-4-20260917175115`.
Task: issue #4, RM + RA. Evidence: `tests/test_research.py`, this document and the
implementation under `src/argos/research/` and `src/argos/backends/`.

Self-review checked role boundaries, failed-task semantics, context isolation,
protected changes and persistence. It added active-project checks, explicit event
validation, decision-to-entity references and compact failure context.

Validation with uv on Python 3.12.3:

- `uv run pytest -q`: **286 passed in 3.29 s**.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: passed (16 files).
- `uv build`: built the source distribution and wheel.
- `git diff --check`: passed.

The commands used `UV_CACHE_DIR=/tmp/argos-issue4-uv-cache` for the sandbox. No
commits, pushes, PRs, issue comments, workflow edits or human approvals were made.
