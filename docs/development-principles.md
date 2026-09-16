# ARGOS Development Principles

## Persistent state is authoritative

Scientific state must survive model sessions and process restarts.

## Agents do not directly mutate persistent state

Agent
→ structured output
→ validation
→ Orchestrator
→ State Store.

## Measurements are deterministic

Evaluator output is authoritative for experimental metrics.
LLMs interpret measurements but do not fabricate or overwrite them.

## Experiment provenance is mandatory

Every experiment must retain enough information to reproduce and audit it.

## Independent research contexts

Independent RAs must not see each other's initial outputs before RM synthesis.

## Failures are data

Execution failures, rejected hypotheses, negative results and failed research directions must not silently disappear.

## Keep v0.1 small

New infrastructure should only be added when required by the minimal end-to-end research loop.
