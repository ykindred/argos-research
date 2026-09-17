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

ARGOS is currently in early development. This checkout defines the v0.1 shared
Pydantic models, protocol examples and validation tests; the runtime is not yet
implemented. See the shared-model documentation for cross-component guarantees
and human approval requirements.

The v0.1 goal is to build a minimal end-to-end research loop with persistent research state, structured agent communication, reproducible experiment execution, objective evaluation, and independent critique.

## Documentation

- [v0.1 Architecture](docs/architecture-v0.1.md)
- [Development Principles](docs/development-principles.md)
- [Shared Models and Protocols](docs/shared-models-v0.1.md)

## Development

Requires Python 3.11+.

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```
