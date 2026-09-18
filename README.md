# ARGOS

**Agentic Research Group Orchestration System**

ARGOS is a lightweight multi-agent research system for advancing human-defined research problems through structured reasoning, experimentation, evaluation, and critique.

## v0.1 Research Loop

Human / Main Research Question

→ Research Manager (RM)

→ Independent Research Agents (RA)

→ Research Manager synthesis and selection

→ Experiment Agent (EA)

→ Deterministic Evaluator

→ Research Manager

→ Critic

→ Next research cycle

## Status

ARGOS is currently in early development. This checkout provides the v0.1 shared
Pydantic models and a SQLite Research State store with provenance, revision
history and bounded snapshots, plus RM/RA roles, independent dispatch, structured
validation and a deterministic fake LLM backend. It also provides implementation-only
coding backends and isolated Git-worktree build/test/run execution with durable
evidence and process timeouts. Deterministic project-command evaluation now adds
strict metrics/constraints, clean baseline execution, pinned comparisons, and
persistent neutral Observations. Independent Critic review adds blind claim/evidence
input, validated verdicts, durable review feedback and deterministic fake tests.
The synchronous Orchestrator now connects these components with durable restart
checkpoints, bounded briefings, human gates, pause/resume and a minimal CLI. An
executable synthetic project exercises the full loop offline. Real research
repository validation and the eight-hour endurance run remain pending.

The v0.1 goal is to build a minimal end-to-end research loop with persistent research state, structured agent communication, reproducible experiment execution, objective evaluation, and independent critique.

## Documentation

- [v0.1 Architecture](docs/architecture-v0.1.md)
- [Development Principles](docs/development-principles.md)
- [Shared Models and Protocols](docs/shared-models-v0.1.md)
- [Research State Store](docs/research-state.md)
- [Research Manager and Research Agents](docs/research-agents.md)
- [Experiment Agent and Isolated Execution](docs/execution.md)
- [Evaluator, Observations and Baselines](docs/evaluation.md)
- [Independent Critic](docs/critic.md)
- [Orchestrator, CLI and Synthetic E2E](docs/orchestrator.md)
- [Issue #8 Validation](docs/issue-8-validation.md)

## Development

Requires Python 3.11+.

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

Run the offline demonstration in a new disposable directory:

```bash
uv run python examples/synthetic/demo.py /tmp/argos-synthetic-demo
uv run argos --project /tmp/argos-synthetic-demo status
```
