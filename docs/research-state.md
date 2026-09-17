# Research State (issue #3)

`argos.state.StateStore` persists the shared Pydantic entities in a local SQLite
file. Python 3.11+ is required. No domain-specific metrics, agent calls, execution
logic or conversation logs are introduced. The user-supplied full architecture,
project-policy.md and task1.md take precedence over the older issue role split.
Those three sources were supplied in the task; separate files are not present in
this checkout. No AGENTS.md was found in the worktree or its ancestor directories.
README, both required docs, shared-model contracts and issues #1–#8 were read.

## Public API

```python
from argos.models import Hypothesis, Project, Run
from argos.state import StateStore

with StateStore("research.sqlite") as store:
    # project is a validated Project supplied by the future ProjectLoader.
    store.create(project)
    # Invoke only after explicit human initialization through a trusted interface.
    store.approve_main_question(
        project.id, project.config.main_research_question,
        rationale="Human approved the initial research question",
    )
    frontier = store.snapshot(project.id, limit=20)
    rejected = store.list(
        Hypothesis, project_id=project.id, status=["rejected", "contradicted"]
    )
    runs = store.list(Run, parent_id=experiment_id, role="experiment_id")
    chain = store.claim_provenance(claim_id)
    history = store.history(Project, project.id)
```

- `create(entity)`, `get(ModelClass, id)`, `update(entity)` use shared models;
  writes revalidate serialized input even if Pydantic validation was bypassed.
- `get` raises `KeyError` for missing IDs or the wrong entity type. Invalid
  relationships/updates raise `StateError`; malformed payloads raise Pydantic
  `ValidationError`. SQLite I/O, lock and constraint errors propagate to the
  orchestrator to handle as infrastructure failures.
- `list` filters by project, status (one or several), or a referenced parent ID.
  `role` selects a field such as `subproblem_id`, `branch_id`, `hypothesis_id`,
  `experiment_id`, `run_id`, `references`, or `output_references`. Rows are ordered
  by latest persisted change, with `limit`/`offset` pagination.
- `transaction()` groups multiple operations atomically; nested operations use
  savepoints. Create parents before children. Wrap read/modify/write operations
  in this context when multiple connections may write, to avoid stale updates.
- `history(ModelClass, id)` returns all persisted revisions, oldest first.
  There is no deletion API. Rejected/contradicted directions remain available even
  after a later status revision. No automatic scientific status changes occur.
- `claim_provenance(id)` returns typed Claim → Evidence → Observation → Run →
  Experiment → Hypothesis paths, including the run's config, commits and log/diff/
  artifact references. It executes in one consistent read transaction.

All twelve entities are supported: Project, Subproblem, ResearchBranch, Hypothesis,
Task, Experiment, Run, Observation, Claim, Decision, Evidence, Baseline. UUIDs and
aware timestamps round-trip without regeneration. SQL stores validated JSON plus
indexed type/project/status/relationship columns. Foreign keys and store checks
reject missing, wrong-type and cross-project references. Branch/subproblem
membership is checked. Parent identities are immutable, preventing cycles and
historical reassignment. Tested hypothesis statements and experiment specs cannot
be rewritten; a changed scientific question uses a new hypothesis/experiment.

Schema version 1 is recorded via `PRAGMA user_version`. Unsupported versions fail
explicitly; no speculative migration framework is included. Each store owns one
single-thread SQLite connection; use separate stores for separate threads or
processes. Explicit transactions and SQLite's FULL synchronous rollback journal
provide local durability. Closing an uncommitted transaction rolls it back.

## Scientific and execution history

Claim writes atomically generate Evidence entities using stable UUID5 IDs for each
claim/observation pair. The evidence lists and entities cannot diverge. Evidence is
additive; to withdraw or reverse evidence, create a new claim and record a Decision
referencing the old claim. The old evidence remains queryable. A claim's relation
to an observation need not equal the observation's relation to its hypothesis:
they can express different scientific statements.

Observation evaluation must exactly equal the successful Run's stored evaluation.
Invalid observations cannot support or contradict claims. Terminal Runs/Tasks,
Observations, Decisions, Evidence and Baselines are immutable. A successful Run
may attach its evaluation once after deterministic execution finishes. Failed Runs
never acquire measurements or automatically contradict a hypothesis. Retries use
new attempt records. Experiment updates follow the defined lifecycle; terminal
experiment retries use new experiment IDs, linked by an explicit Decision.
Deterministic build failure during testing maps to `implementation_failed`.
Creation may import a validated terminal record without replaying intermediate
states; the executor remains responsible for persisting live phase transitions.

`record_review(review, reviewed_claim=claim)` appends a review and the exact claim version reviewed,
returning a stable review UUID. `reviews(project_id, claim_id=..., limit=...)`
returns newest reviews first. All verdicts and reasons remain stored; recording a
review does not automatically accept/reject a claim. Stale reviews are rejected
if the claim changed after its review input was prepared. This is persistence support
for issue #7, not a Critic agent implementation.

## Canonical question and baseline protection

A new Project defaults to `main_question_approved=False`; its config question is
a draft. `proposed_main_research_question` holds proposed replacement wording.
Generic updates may change the proposal, name or project status, but cannot change
config, approval state or the active baseline. A snapshot exposes
`main_research_question=None` until initial approval; after approval it continues
to expose the canonical question while proposals remain pending.

`approve_main_question(project_id, exact_question, rationale=..., cycle=...)`
is a **trusted human-interface entry point**. It requires the exact current draft
or proposal and atomically persists a human Decision with the canonical update.
Project history retains the old question. A forged `Decision(actor="human")` does
not grant generic update authority. Other config changes remain blocked in this
issue; a later trusted human-interface implementation can provide audited
operations for them. Experiments cannot replace the project's evaluator protocol.

`approve_baseline(baseline, rationale=..., cycle=...)` similarly records a human
Decision, inserts an immutable baseline and updates the current project pointer in
one transaction. Its approval decision ID must be new; previous_baseline_id must
match the current baseline. The evaluation must match a successful stored run
with unchanged before/after source commits. Past baseline snapshots and pinned
experiment/evaluation baseline IDs are preserved. Generic baseline creation and
pointer replacement are rejected.

These methods do not authenticate a person: only trusted CLI/orchestrator code
may call them after receiving explicit human input. Never expose the store or its
human methods as agent tools or dispatch them based on an agent's actor label.
Clean-worktree verification, permission enforcement and artifact-file retention
belong to the loader/executor, not SQLite. This issue neither runs evaluators nor
claims a stored source commit proves the checkout was clean.

## Bounded frontier

`snapshot(project_id, limit=20)` uses one read transaction and returns a typed
StateSnapshot with active subproblems/branches, current hypotheses, recent
experiments/observations, candidate and rejected claims, failed directions,
abandoned branches, failed runs, tasks, decisions and reviews. Each section is
bounded independently (1–1000 rows); `truncated` names sections with more data.
Ordering uses durable write sequence rather than caller timestamps. Full state
remains available through list/history queries.

This is a structured RM query, not the issue #8 BriefingBuilder: it bounds entity
counts, not text tokens. The orchestrator must trim/select necessary context and
must not hand this project-wide snapshot to independent or blind RAs. Context
policy is persisted; context isolation and event-driven RM invocation remain
runtime responsibilities.

## Validation and limitations

Tests exercise all entities, actual subprocess exit/reopen and crash rollback,
foreign keys, nested transactions, duplicate/missing/wrong-project references,
canonical question approval, rollback on failed approval, baseline refresh history,
claim provenance/projection, immutable measurements, negative versus failed runs,
invalid evaluation, failed tasks, review version retention, lifecycle transitions,
revision history, bounded snapshots and multi-connection visibility.

No model API, runtime E2E, real research repository, physical machine restart,
clean-worktree execution, human interface approval or ≥8-hour run was performed.
Those are downstream/runtime or human-review checks. SQLite persistence does not
resume processes or retry tasks; the orchestrator must interpret pending/running
records after restart. Human review of these shared interfaces remains pending
for the PR.

### Revision validation and handoff

Project: ARGOS (`argos-research`), branch `codex/issue-3-20260917172145`.
This revision preserves the existing issue #3 implementation and updated shared
contracts. Self-review found that a running Task could reset its repair counter
or replace its dispatch inputs. Task kind, resource class and input references are
now immutable; its repair counter cannot decrease and its start timestamp cannot
change once recorded. Completed tasks require a start timestamp; execution times
cannot precede task creation. Pre-dispatch failures may still omit a start time.
The failed-task example publishes no outputs and preserves the one repair attempt.
No SQLite schema migration is needed; existing valid records retain their IDs.
Older task payloads with inconsistent timing now fail validation when read; they
are not silently rewritten or deleted.

The orchestrator must persist the repair increment **before** calling the repair
backend, then record failure after a second invalid response. The store cannot
count external calls or prevent a caller from creating a new task ID. Retry policy,
atomic publication of validated outputs, event delivery and restart recovery remain
cross-component guarantees. Human approval operations still require a trusted
human interface; no agent-supplied label authenticates approval.

Evidence: `tests/test_models.py`, `tests/test_state.py`, and
`examples/entities.json` (`failed_task`). Tests cover rejection without changing
history, close/reopen after repair, durable failure, and unchanged scientific state.
Final validation with uv on Python 3.12.3:

- `uv run pytest -q`: **260 passed in 2.27 s**.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: passed (8 files).
- `git diff --check`: passed.

The initial format check found two test-layout differences; formatting was applied
and the final check passed. Packaging was not rerun for this revision.

No runtime E2E, real research repository, physical reboot, human approval, or
≥8-hour run is claimed. Human interface review remains pending for the PR.
