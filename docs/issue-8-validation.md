# Issue #8 implementation and validation

Validated on 2026-09-18 with uv and Python 3.12.3 on Linux. The implementation
continues the pre-existing unfinished worktree and uses the provisional shared
component interfaces. No research direction or project-specific integration was
added to the platform.

## Acceptance coverage

| Requirement | Implementation / exercised evidence |
| --- | --- |
| Complete synchronous loop | Orchestrator dispatches RM → independent RAs → RM synthesis → ExperimentSpec → coding → deterministic execution/evaluation → Observation → RM claim → Critic → RM |
| Three independent RAs and join | Frozen per-task contexts, falsification perspective, synthesis after all outcomes (including failures); integration assertions inspect actual requests |
| Measurements precede interpretation | SQLite Observation commit before RM event; neutral relation; evaluator owns metrics/constraints |
| Claim and Critic feedback | Persisted claim evidence chain and blind review; `needs_more_evidence` requested checks appear in the next RM briefing |
| Human gate | Proposed question remains unapplied until trusted accept/edit; reject and stale/replayed answer tests; protected evaluator changes cannot be approved through generic answer |
| Component failure isolation | Invalid RA/Critic output, coding/test failures, invalid/mismatched evaluation; failure retained without falsifying hypothesis; explicit successful second attempt after failure |
| Pause, resume and restart | SQLite reopen, subprocess exit, killed runner with live child, saved manager/RA/EA/evaluator/Critic outcomes, no automatic replay of uncertain external work |
| Limits | Configured decision-cycle, experiment-attempt and stagnation gates; shared finite resource semaphores |
| Fake and executable E2E | FakeLLMBackend/FakeCodingBackend/FakeEvaluator integration and real synthetic Python commands in temporary Git worktrees |
| Provenance | Baseline history, isolated worktrees, source/resulting revisions, configuration, patches, command records, raw streams and evaluator artifacts; failed/interrupted evidence retained |

`tests/test_orchestrator.py` covers the complete integrated loop and CLI.
`tests/test_runtime_edges.py` covers process interruption, outcome persistence
failures, backend command adapters, recovery, protected settings and explicit
retries. Existing component tests also run in the complete suite.

## Self-review fixes

Review covered role boundaries, branch isolation, persistence, evaluator authority,
protected settings and failure semantics. The final review fixed:

- Partial manifest publication: baseline and evaluator JSON now use atomic rename.
- Missing baseline command exit records during interrupted recovery: each completed
  or cancelled command is saved before proceeding.
- Validly shaped evaluator results for a different baseline/protocol: recorded as
  invalid component results instead of escaping into a state-store error.
- Truncated evaluation manifests from interrupted/older writers: preserve the file,
  record invalid evaluation, and return control to RM without fabricating evidence.
- Failed evaluation Tasks use the experiment's resource class.

Regression tests simulate interruption immediately before manifest publication,
verify command exit codes survive baseline recovery, and check mismatched and
truncated evaluation handling. Actual subprocess termination is also exercised;
simulated interruptions are not counted as long-run evidence.

## Executed validation

- `uv run pytest -q`: **426 passed in 43.64s**.
- `uv run pytest -q tests/test_runtime_edges.py`: **22 passed in 14.04s**.
- `uv run ruff check src tests examples`: passed.
- `uv run ruff format --check src tests examples`: passed, 48 files formatted.
- `uv lock --check`: passed, 14 packages resolved.
- `git diff --check`: passed.
- `uv run python examples/synthetic/demo.py .argos/issue8-validation`: completed,
  8 RM decision cycles, 2 Observations (baseline plus experiment), 1 Critic review.
- `uv run argos --project .argos/issue8-validation status`: persisted completed
  state and 8 cycles confirmed from a separate CLI invocation.

The standalone demo's local evidence is retained under
`.argos/issue8-validation/.argos/` (ignored runtime data). Its final Critic verdict
is `needs_more_evidence`; successful orchestration does not imply the scientific
claim has been accepted.

## Pending / limitations

- No eight-hour endurance run or real research repository validation was performed.
- No paid/real model backend invocation was performed. The command backend interface
  was exercised using local offline adapters; provider setup remains external.
- Human review and interface approval remain pending for the supervisor's PR.
- Linux orphan recovery and local advisory locking are the supported host behavior.
  Cooperative worktrees/process groups are not a security sandbox; deliberately
  detached children and unidentifiable orphan groups require human inspection.
- Atomic file publication is tested for process interruption, not machine power loss.
  Persistence failures stop the runner with retained checkpoints for inspection.
- No automatic worktree cleanup, distributed pipeline, web UI, RAG or paper generation
  was added. No project commits, pushes, PRs, issue updates or messages were made by
  this task; synthetic tests create commits only in disposable fixture repositories.
