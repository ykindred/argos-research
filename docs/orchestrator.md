# Orchestrator and synthetic E2E

Issue #8 connects the shared components into a synchronous, event-driven loop.
SQLite is authoritative; no conversation transcript is required to restart.
The host dispatches structured actions and records decisions. Scientific choices
remain with RM, measurements with the deterministic evaluator, and protected
research settings with the human.

## Running the offline example

Requires Python >=3.11, uv, Git, and Linux for verified orphan-process recovery.
From the ARGOS checkout:

```bash
uv sync --extra dev
uv run python examples/synthetic/demo.py /tmp/argos-synthetic-demo
uv run argos --project /tmp/argos-synthetic-demo status
```

Use a new destination. The demo creates a disposable Git repository, establishes
an unchanged baseline, dispatches three independent RAs (including falsification),
joins their results for RM synthesis, implements a set-based distinct-count
algorithm in an isolated worktree, and executes the protected tests and evaluator.
It records comparisons, elapsed time and peak traced Python memory, then forms a
scoped claim. The scripted Critic requests more evidence; RM receives that request
and stops the bounded demo with those checks still pending. This is a test fixture,
not evidence of autonomous scientific reasoning or general performance gains.
The fake-evaluator test also exercises the loop with fully scripted measurements.

The destination retains `.argos/state.sqlite`, baseline and experiment worktrees,
source/resulting revision IDs, configuration, patches, command logs, evaluator
inputs/results and backend diagnostics. The source fixture stays unchanged.

## Project configuration and CLI

A project directory contains `research.md`, `project.yaml`, and its own Git source
repository. Use [the synthetic configuration](../examples/synthetic/project.yaml)
as a complete example of the shared `ProjectConfig` YAML schema. `research.md`
provides the charter; `main_research_question` in YAML is the canonical question.
Paths to the source repository are resolved relative to the project directory.
State must live outside that source repository. Platform code remains separate.

```bash
uv run argos --project /path/to/project init
uv run argos --project /path/to/project status
uv run argos --project /path/to/project inspect ENTITY_UUID
uv run argos --project /path/to/project pause
uv run argos --project /path/to/project resume
uv run argos --project /path/to/project answer GATE_UUID 'Keep the current question'
uv run argos --project /path/to/project answer GATE_UUID 'Approve revision' --approve
uv run argos --project /path/to/project answer GATE_UUID 'Use my wording' --approve --edit-question 'New question'
uv run argos --project /path/to/project baseline refresh --reason 'Human requested new measurement'
```

`init` is a trusted human action approving the configured question and establishing
a clean baseline. Baseline refresh is explicit, retains previous baselines, and
refuses to overlap pending research work. Failed initialization remains paused;
use explicit baseline refresh to recover it. Editing YAML after initialization
does not silently change persisted settings. Direction, evaluator and held-out
protocol changes require explicit host reconfiguration; the generic gate cannot
approve those changes. Question revisions support accept, reject and edited
approval with an audited decision and stale-answer protection.

`pause` is accepted while the local runner owns its lock. An in-flight operation
finishes its durable transition before the loop stops. `resume` changes state;
invoke `run` again to execute. Pending human gates require `answer` first. Maximum
RM decision cycles, maximum experiment attempts and stagnation cycles come from
`config.limits`. Each RM planning/synthesis call consumes one decision cycle;
exhaustion opens a human gate, and answering does not reset the hard budget.

## Backend boundary

```bash
uv run argos --project /path/to/project run \
  --backend-command '["/absolute/path/to/llm-adapter"]' \
  --coding-command '["/absolute/path/to/coding-adapter"]'
```

The command adapters use explicit argv with JSON on stdin and a single JSON object
on stdout, with logs on stderr. The LLM adapter receives `LLMRequest` (role, prompt,
context, schema, optional repair error); its output matches the supplied schema.
The coding adapter receives `{system_prompt, task}`, runs inside its assigned
worktree, returns `CodingResult`, and exits. Build/test/run/evaluate then run as
ordinary deterministic code. No coding tool loop or provider routing is added.
Provider setup is the host adapter's responsibility. A real provider has not been
validated by this issue; the command bridge is tested with local offline scripts.

For an offline CLI run, use `examples/synthetic/adapter.py` as the LLM command and
`--fake-files /absolute/path/to/files.json` for an explicit file-content mapping.
`--steps N` stops after N persisted transitions without changing project status.

## Persistence and failure semantics

- A local runner lock prevents concurrent orchestration or baseline refresh.
  Separate finite semaphores bound LLM, coding, CPU and GPU work. Execution and
  evaluation share CPU/GPU semaphores.
- Initial RA inputs are frozen before dispatch; outputs join before synthesis.
  Structured output gets at most one repair. Invalid responses become failed Tasks.
- Briefings contain at most eight records per frontier section and five decisions,
  without execution streams or full history. A 60,000-character call guard rejects
  oversized context instead of silently dropping scientific evidence.
- Each external operation is journaled before dispatch. Completed LLM outputs are
  persisted before actions are applied. Execution/evaluation manifests are published
  by atomic rename; SQLite transitions and cursor updates commit together.
- On restart, completed outputs/manifests are imported once. Uncertain interrupted
  work is recorded as failure, with its worktree, patch and raw logs retained; it is
  not automatically replayed. RM must explicitly propose another experiment.
- Linux process markers record boot/start identity before waiting for commands.
  Recovery kills verified orphan process groups and refuses to signal reused PIDs.
  If an orphan group cannot be identified safely, human inspection is required.
- Execution failures and invalid evaluations never falsify hypotheses. Invalid
  evaluation creates no Observation. Valid measurements are persisted before RM
  continues; scientific interpretation and claims remain RM decisions.
- Critic receives claim/evidence context without RM reasoning. Its review is durable,
  and `needs_more_evidence` returns control to RM with requested checks.

The runtime is for cooperative local backends, not an OS security sandbox. Process
recovery is Linux-specific; process groups do not contain deliberately detached
children. Atomic rename and SQLite recovery cover process interruption, not a tested
power-loss guarantee. Disk/persistence errors stop with checkpoints intact rather
than pretending that research state was saved. Worktrees/evidence are retained;
there is no automatic evidence deletion.

## Validation

See [issue #8 validation](issue-8-validation.md) for actual checks and pending
acceptance work. No eight-hour run, real research repository validation, real model
API execution or human interface approval is claimed.
