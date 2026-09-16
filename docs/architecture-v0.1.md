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

Formal experiment execution belongs to EA.

## 5. Experiment Agent

EA executes an explicit ExperimentSpec.

Responsibilities:

- understand the requested experiment;
- modify code;
- build;
- test;
- run the experiment;
- preserve logs and artifacts;
- return ExperimentResult.

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
- ResearchBranch
- Hypothesis
- Experiment
- Run
- Observation
- Claim
- Decision

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
