# EA and local execution (issue #5)

`argos.execution.ExperimentAgent` accepts the existing validated `ProjectConfig`
and `ExperimentSpec`. `argos.backends.coding.CodingBackend` is an async,
implementation-only interface with a concise built-in `CODING_PROMPT`.
`FakeCodingBackend` writes deterministic files without an LLM or network service.

```python
from pathlib import Path
from argos.backends.coding import FakeCodingBackend
from argos.execution import ExperimentAgent

# project and spec are validated shared protocol objects from the host.
agent = ExperimentAgent(
    project,
    FakeCodingBackend({"src/example.py": "print('example')\n"}),
    Path("/tmp/argos-experiments"),  # outside the canonical source checkout
    command_timeout_seconds=30,
)
result = await agent.execute(spec)
```

Every submission gets a new run UUID, including an explicit retry of the same
experiment. Source is the canonical repository's committed HEAD: uncommitted
canonical edits are neither used nor changed. The executor creates a detached
worktree, calls the coding backend once, validates its result and actual diff,
records the implementation revision, then runs the spec's build/test/run argv
lists in that workspace. Empty build/test lists skip those phases. Project
commands can be copied into those lists by the host; the explicit spec determines
the commands actually executed. No implicit shell expansion occurs.

EA exits before deterministic execution. A failure never calls coding again;
the caller must decide whether to submit another attempt. Execution success does
not assert scientific success. Evaluator invocation, metric parsing, baseline
selection, scientific interpretation and Observation creation remain issue #6
and orchestration responsibilities. In particular, a successful command reporting
a worse metric still yields successful execution here.

## Policy and resources

The project evaluator protocol cannot be replaced by the spec. Project and spec
editable scopes are intersected when checking implementation changes; protected
paths from either scope take precedence. Paths use repository-relative,
case-sensitive `fnmatch` patterns (`*` can match `/`); literal directory names also
cover descendants. Absolute paths, `..`, `.git` path components and backslashes
are rejected. Tracked edits, deletions, new and ignored files are inspected.
Changed symlinks are rejected. Commands may generate output files but cannot
change the recorded implementation or protected files. The executor computes
provenance itself; a backend-provided diff is not authoritative.

`.argos-coding/` is reserved for backend diagnostics, excluded from source
snapshots, and copied into evidence even after backend exceptions/cancellation.
Backend log references must resolve to regular files within the assigned attempt.
`CodingTask.project_scope` is an optional additive field populated by the executor
so adapters see both policies, including when the spec narrows or attempts to
broaden editable patterns. Adapters must use the supplied workspace, honor both scope policies, use
`CODING_PROMPT`, avoid Git commits/metadata changes, honor async cancellation and
terminate any subprocesses they launch (the supplied ProcessRunner is reusable).
No real coding backend is integrated in this issue.

The minimum of project/spec wall-clock limits covers queue waits, coding and
commands. An optional per-command limit further bounds each command. Separate
finite `asyncio.Semaphore` queues limit coding, CPU and GPU execution; share the
agent instance across submissions. LLM limits remain in `StructuredCaller`.
POSIX commands run in their own sessions: timeout/cancellation kills the process
group and waits for the parent. Background children are also stopped after a
normally exiting parent. A child deliberately escaping its session is outside
this mechanism. Local Git operations have a separate 30-second administrative
timeout; final evidence preservation runs even after the execution budget expires.
Memory/core fields are recorded but OS memory and CPU quotas are not enforced.

This is workspace isolation and post-execution permission validation, **not an
OS security sandbox**. Trusted local backends and project commands can access
files with the host user's permissions; arbitrary absolute writes or malicious
Git metadata changes are not prevented. Deploying untrusted commands requires
external sandboxing. There is no distributed execution.

## Durable evidence and cleanup

Each attempt has sibling `worktree/` and `evidence/` directories. Evidence includes:

- `configuration.json`, including complete project/spec and command timeout;
- `events.jsonl`, recording execution phases and terminal outcome;
- `code.diff`, a binary-capable patch against the source, including untracked and
  ignored outputs so partial failures remain reconstructable;
- per-command argv, raw stdout/stderr, exit-code records, and aggregate logs;
- backend diagnostics and copies of requested artifacts;
- `result.json`, the terminal shared `ExperimentResult`, published by atomic rename.

`ExperimentSpec.required_artifacts` is an additive optional list (default empty)
of regular workspace-relative files. Missing required files produce
`missing_artifact`; permission violations produce `invalid_modification`. Both are
new explicit execution failure kinds. Available requested files are copied on
failure as well as success. Unsafe/symlink artifact paths are rejected, not read.

A modified accepted implementation gets a local Git commit and a retained
`refs/argos/runs/<run UUID>` reference. No canonical branch is advanced and no
push occurs. An unchanged implementation records the source commit as its result.
Rejected or incomplete implementations retain their patch and may have no
resulting commit. If source resolution fails, `source_commit` is `unavailable`
and the result is explicitly failed. Failure messages and phase history retain
that cause; this marker must never be treated as a reproducible source revision.

Worktrees are retained by default. Explicit cleanup is:

```python
agent.worktrees.cleanup(Path(result.worktree), Path(result.diff_path).parent)
```

Cleanup requires a matching terminal manifest and external evidence files, and
refuses removal if diff preservation reported an error. It removes the worktree
only; evidence and successful revision refs remain. Git reference pruning and
evidence retention policies are deliberately not automated.

The host can persist returned results as `Run.result` through StateStore; the
executor does not mutate scientific state. Caller cancellation records a failed
manifest before propagating cancellation. Durable command logs and phase records
remain after abrupt termination, but reconciling interrupted attempts with SQLite,
pause/resume and restart recovery belongs to issue #8. Disk/persistence failures
can still raise: the host must not report completion when evidence cannot be saved.

## Validation and handoff

Tests use actual temporary Git repositories and local Python subprocesses. They
cover successful and failed build/test/run, code revisions, canonical checkout
isolation, scope/evaluator protection, invalid commands/results, ignored/new/
deleted/binary files, artifacts on success and failure, timeout and cancellation,
process descendants, repeated attempts, resource queue bounds and explicit cleanup.
These are execution integration tests, not the complete synthetic research E2E.

Self-review checked role boundaries, failure semantics, isolation and persistence.
It fixed new-file index handling, retained revision refs after cleanup, and ensured
cancellation retains actual process exit codes and backend diagnostics. Human
interface review is pending. No real backend, real research-repository validation
or eight-hour run is claimed.

Handoff: ARGOS (`ykindred/argos-research`), branch
`codex/issue-5-20260917180834`, issue #5.
Runtime evidence is at `<storage>/<experiment UUID>/<run UUID>/evidence/`.

Actual validation on Python 3.12.3 using uv (2026-09-18):

- `uv run pytest -q`: **323 passed in 10.87 s**, including 35 execution tests.
- `uv run ruff check src tests`: passed.
- `uv run ruff format --check src tests`: passed (22 files).
- `git diff --check`: passed.

The uv cache was redirected to `/tmp/argos-issue5-uv` for worktree permissions.
No ARGOS repository commit, push, PR, workflow edit or GitHub message was made.
Git commits created by integration tests exist only in temporary fixture repositories.
