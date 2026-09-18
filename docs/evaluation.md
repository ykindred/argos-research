# Deterministic evaluation and Observations

`argos.evaluation` implements issue #6. Evaluators measure objective outcomes;
Research Manager interprets their scientific meaning. There are no LLM calls,
scientific verdicts, coding retries, or automatic baseline refreshes here.

## Project command and output

The protected `ProjectConfig.evaluation_protocol` supplies an explicit argv,
protocol name, required metric names, and required constraint names. The
`ExperimentSpec` must use that same protocol. `CommandEvaluator` runs the command
in the successful execution's retained Git worktree, after coding/build/test/run
have finished. It rejects failed execution, mismatched experiment IDs, changed
recorded specifications, and tracked-source/revision drift. Evaluate before
cleaning up the execution worktree.

The command prints exactly one JSON object to stdout; diagnostics go to stderr:

```json
{
  "status": "ok",
  "metrics": {
    "runtime": {"value": 8.0, "unit": "second", "direction": "minimize"},
    "quality": {"value": 0.98, "direction": "maximize"},
    "sample_count": {"value": 100}
  },
  "constraints": {
    "correctness": true,
    "quality_floor": {"passed": true, "measured_value": 0.98, "threshold": 0.97}
  },
  "artifacts": ["benchmark.log"]
}
```

Metric names and units are project-defined. Directions are `minimize`, `maximize`,
or `informational` (the default). Numbers must be finite JSON numbers, not strings
or booleans. Constraint booleans are strict. Constraint arithmetic belongs in the
project's deterministic evaluator; ARGOS does not interpret constraint-name text.
Additional metrics/constraints are allowed; configured required names must exist.
Unknown fields, duplicate JSON keys, whitespace-padded keys, malformed output,
missing metrics/constraints, nonzero exit, and missing/unsafe artifacts are
explicit evaluation failures. Artifact names must identify regular files within
the worktree; symlinks, traversal, and absolute paths are rejected.

The built-in parser is `argos-json-v1`. A project with another output format should
configure an evaluation command that parses its own files and emits this JSON.
No project-specific parser or metric logic belongs in the ARGOS core.

## Calling and persisting

The evaluator implements this interchangeable interface:

```python
async def evaluate(spec, result, *, baseline=None) -> EvaluatorResult: ...
```

The host loads a pinned baseline, if any, from SQLite, then calls:

```python
from argos.evaluation import CommandEvaluator, record_evaluation
from argos.models import Baseline

baseline = store.get(Baseline, spec.baseline_id) if spec.baseline_id else None
evaluator = CommandEvaluator(project.config, evaluation_storage)
evaluation = await evaluator.evaluate(spec, execution_result, baseline=baseline)
observation = record_evaluation(store, evaluation)
```

`evaluation_storage` must be outside the source checkout and experiment worktree.
The host must first persist the Experiment and successful Run, and move the
Experiment to `running` or `evaluating`. `record_evaluation` atomically attaches
the result, completes the evaluation lifecycle, and creates one neutral
Observation with a reproducible UUID. Replaying the same result is idempotent;
replacing it is forbidden. The Observation embeds the result and references Run;
Run references Experiment, whose spec references Hypothesis. SQLite retains that
whole provenance chain across restarts.

A failed correctness constraint is still valid measurement data: status `ok`,
constraint `passed=false`, neutral Observation. It does not automatically change a
Hypothesis or Claim. Malformed/crashed/timed-out evaluation produces status
`invalid`, diagnostic `failure`, and no measurements, comparisons, or Observation.
It stays on the Run; Experiment becomes `invalid_result` or `timeout`. External
cancellation preserves an invalid manifest before propagating cancellation to the
host. Invalid caller contracts raise `ValueError` before any command is launched.

## Baselines

`BaselineRunner(project.config, baseline_storage).execute(spec)` starts from the
canonical repository's committed HEAD in a **new clean detached worktree**. Dirty
canonical files are not included. It runs the supplied build/test/run steps with
no CodingBackend and does not apply `requested_change`. The caller supplies an
explicit baseline ExperimentSpec with the project's protected evaluation
protocol, then evaluates and persists the returned result in the same way as any
other experiment. Source modifications invalidate baseline execution; generated
outputs are allowed and required artifacts are retained. The original source
checkout is untouched. Failed worktrees, patches and logs are retained.

Only a trusted human entry point should call:

```python
from argos.evaluation import approve_baseline

baseline = approve_baseline(store, project.id, baseline_run.id, rationale="Human initialization")
```

This requires a successful clean baseline run, valid evaluation, and passing
constraints. The same explicit operation performs human refresh after a new
baseline run. It records a human Decision and appends a new Baseline linked to its
predecessor. It never rewrites previous snapshots or experiment baseline IDs.
It must not be exposed as an agent tool; human authentication/CLI wiring belongs
to the host. The existing lower-level `StateStore.approve_baseline` remains a
trusted persistence primitive, not a clean-checkout executor.

An experiment must receive its exact pinned baseline. Shared metrics require
matching units and optimization directions, and the baseline must contain every
required metric. Comparisons record current/baseline values, `delta = current -
baseline`, and `percent_change = delta / abs(baseline) * 100`. A zero baseline has
no percent change (`null`). Additional metrics without a baseline have no
comparison. Arithmetic overflow is invalid evaluation, never infinity-valued
evidence. Direction is metadata, not a scientific success judgment.

## Evidence and resource boundaries

Every evaluation invocation has a unique directory containing:

- `input.json`: full spec, execution result, project config, pinned baseline;
- stdout/stderr, actual argv/exit status in the result;
- `source.diff`: tracked changes relative to the execution revision;
- copied project artifacts;
- `evaluation.json`: validated result, comparisons, provenance and failure.

Baseline execution similarly retains config, command logs, patches (including
untracked outputs), required artifacts and `result.json`. No automatic worktree
cleanup is performed. Source commit IDs, execution artifacts and evaluation
inputs make reproduction possible subject to the project's external data and
environment remaining available.

CPU/GPU semaphores are finite and configurable. The host can inject shared
`cpu_sem`/`gpu_sem` into both evaluator and baseline runner, including semaphores
from `ExperimentAgent`, to enforce one common execution budget. Timeout includes
queue wait and subprocess execution; local Git operations use the execution
module's separate 30-second guard. Process-tree termination uses the existing
POSIX runner. Commands are trusted project code, not an OS security sandbox;
memory/core hard limits and global scheduling are not added by this issue.

`FakeEvaluator(project, storage, output_json)` substitutes fixed stdout without
launching the evaluator command, while using the same parser, comparisons,
provenance, artifact checks, and successful-worktree input contract. It needs no
LLM/API credentials and can be composed with FakeCodingBackend.

## Validation and remaining integration

`tests/test_evaluation.py` covers strict parsing, finite numbers, multiple metrics,
constraint failures, real command crashes/timeouts/cancellation, queue timeout,
source drift, unsafe artifacts, clean baselines, explicit refresh history,
pinned comparisons, SQLite restart, atomic attachment, and execution → evaluation
→ Observation with a fake coding backend and real deterministic command.

CLI initialization/refresh, full project loading, orchestrator dispatch/recovery,
and the full synthetic research/critic loop remain issue #8 integration work.
No real research repository run, human interface approval, or eight-hour run is
claimed by these bounded tests.

Validation performed for this implementation (Python 3.12.3, uv 0.12.13):

- `uv run pytest -q`: **367 passed in 20.41s**, including 44 evaluation tests.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: passed (28 files).
- `git diff --check`: passed.

Commands used `UV_CACHE_DIR=/tmp/argos-issue6-uv-cache`. Self-review checked
measurement/interpretation boundaries, timeout/cancellation handling, artifact
isolation, append-only baselines, provenance and SQLite rollback/restart. Review
found and fixed timeout cancellation normalization and failure-diff preservation;
regression tests cover both. New EvaluatorResult fields have defaults so existing
stored JSON remains readable without a SQLite schema migration.

Handoff: ARGOS (`ykindred/argos-research`), branch
`codex/issue-6-20260917182552`, issue #6 Evaluator + Observation. Implementation is
under `src/argos/evaluation/`; test evidence is `tests/test_evaluation.py` and the
actual results above. Human review of the
provisional shared-interface additions remains pending for the supervisor's PR.
