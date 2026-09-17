# ARGOS v0.1 Architecture

## 1. Goal

Given:

- a human-defined main research question;
- an existing research codebase;
- an executable evaluation protocol;

ARGOS should maintain a minimal autonomous research loop:

Main Question
→ RM
→ independent RAs
→ RM synthesis
→ ExperimentSpec
→ EA
→ Evaluator
→ Observation
→ RM interpretation / next action
→ Candidate Claim → Critic (when needed)
→ next research cycle.

## 2. Research hierarchy

ARGOS separates research problems into four levels:

1. Research direction
   - decided by humans / advisor.

2. Main research question
   - decided by humans;
   - RM may challenge, clarify, and propose revisions;
   - any revision requires explicit human approval.

3. Subproblem
   - created and managed primarily by RM.

4. Idea / Hypothesis
   - proposed primarily by RAs;
   - synthesized, selected and managed by RM.

## 3. Research Manager

RM is the scientific control center.

Responsibilities:

- inspect current Research State;
- decompose the main research question;
- create and prioritize subproblems;
- dispatch independent RA exploration;
- synthesize RA outputs;
- deduplicate and combine ideas;
- select hypotheses worth validating;
- design experiments;
- interpret observation;
- decide the next research action;
- decide when an important claim should be reviewed by Critic.

RM does not directly execute shell commands or mutate persistent state.

## 4. Research Agents

RAs perform independent idea exploration.

Different RAs may start from different perspectives:

- literature / prior work;
- source-code analysis;
- profiling and observations;
- theoretical reasoning;
- free exploration;
- falsification.

Initial RAs must not see each other's outputs.

RAs primarily produce:

- ideas;
- hypotheses;
- rationale;
- expected effects;
- possible validation methods.

EA implements experiments; deterministic execution code runs them after EA exits.

## 5. Experiment Agent

EA implements the requested change from an explicit ExperimentSpec.

Responsibilities:

- understand the requested experiment;
- modify code;
- perform necessary implementation repairs;
- return CodingResult and exit.

Deterministic execution code then builds, tests, runs and evaluates the experiment,
preserves logs/artifacts, and returns ExperimentResult. A failure requires an
explicit decision before invoking EA again.

EA must not decide whether a scientific hypothesis is true.

## 6. Evaluator

Evaluator is deterministic.

Responsibilities:

- execute or parse the project-defined evaluation protocol;
- produce objective metrics;
- check explicit constraints.

Evaluator measures.

Evaluator does not perform scientific interpretation.

## 7. Critic

Critic independently attacks important candidate claims.

Critic receives only the evidence necessary to review the claim, such as:

- main research question;
- candidate claim;
- ExperimentSpec;
- relevant code diff;
- raw experimental results;
- evaluation protocol.

Critic should not receive RM's private reasoning history.

Possible results:

- accept;
- reject;
- needs_more_evidence.

Results should include:

- a concise rationale;
- identified weaknesses or risks;
- specific additional checks when applicable.

## 8. Research State

Scientific memory must not depend on conversation history.

Core entities:

- Project
- Subproblem
- ResearchBranch (independent/shared/blind context policy)
- Task
- Hypothesis
- Experiment
- Run
- Observation
- Claim
- Decision
- Evidence
- Baseline

Every important scientific conclusion must be traceable to observations, runs, and experiments.

## 9. Structured agent protocol

Agents never directly mutate persistent state.

LLM
→ structured output
→ Pydantic validation
→ Orchestrator
→ State Store.

## 10. Experiment isolation

Experiments should execute in isolated Git worktrees.

Every Run records:

- source commit;
- resulting commit;
- configuration;
- command;
- stdout/stderr;
- metrics;
- artifacts.

## 11. Failure semantics

ARGOS distinguishes execution failure from scientific negative results.

An execution failure includes cases such as:

- build failure;
- test failure;
- timeout;
- invalid evaluator output;
- runtime crash.

A scientific negative result occurs when an experiment executes successfully but its observations contradict or fail to support the tested hypothesis.

Both execution failures and negative results must be persisted.

Execution failure must never be interpreted as falsification of a scientific hypothesis.


## 12. Project interface

A research project provides ARGOS with the information required to reason about and execute experiments.

At minimum, a project should define:

- research context;
- main research question;
- source repository;
- build command;
- test command;
- evaluation command;
- editable / protected paths;
- experiment resource limits.

Project-specific configuration should remain separate from ARGOS core logic.

## 13. v0.1 execution model

ARGOS v0.1 uses a synchronous research loop.

Each research cycle follows the RM → RA → RM → EA → Evaluator → RM
control flow.

Ideas and experiment validation are not maintained as independent
asynchronous pipelines.

## 14. v0.1 non-goals

The following are explicitly deferred beyond v0.1:

- asynchronous Idea Bank;
- CORAL integration;
- multiple experiment execution engines;
- distributed execution;
- semantic / vector research memory;
- web dashboard;
- automatic paper generation.

## 15. Updated shared-contract boundaries

The full user-provided architecture and project policy supersede conflicting old
role descriptions. Human approval protects direction, main question, evaluator,
baseline and held-out protocol. RM is triggered by completed events with a bounded
briefing and performs synthesis itself. At least three independent RA branches
are supported, including falsification. Logical parallel exploration and separate
finite LLM/coding/CPU/GPU semaphores coexist with synchronous research cycles.

See [shared models](shared-models-v0.1.md) for Task, full experiment lifecycle,
baseline history, validity semantics, and the remaining runtime guarantees. These
contracts alone do not implement runtime enforcement or human authentication.
SQLite persistence and audited trusted approval entry points are provided by
[StateStore](research-state.md); runtime human input remains a separate requirement.
