# Independent v0.1 review

Review covered role boundaries, independent exploration and synthesis, protected
project settings, deterministic evaluation, process recovery, persisted evidence,
CLI configuration and separation of platform logic from external research tasks.
Source inspection found no external benchmark-specific algorithm or metric logic
in the platform. Synthetic fixture decisions remain in examples and tests.

## Findings and fixes

- The CLI silently imposed a 60-second LLM command deadline. It now forwards the
  project's configured timeout; the CLI integration test inspects the actual value.
- A malformed execution manifest prevented restart recovery from completing.
  Recovery now retains the invalid original and records the interrupted run as
  failed without replaying implementation or measurement. Recovered manifests use
  atomic publication. Regression coverage includes experiments and baseline setup.
- A real-model synthetic trial repeatedly returned `synthesize` during synthesis
  until the stagnation gate paused the project. The role prompt now identifies the
  active synthesis step; validation rejects requesting it again and allows only
  the existing single output-repair attempt. Both successful and failed repair
  cases are covered without applying scientific state inside a role.

## Verification status

The final review revision passed 430 tests in 40.27 seconds, Ruff lint/format
checks, and git diff checks. Regression cases cover CLI deadline propagation,
corrupt experiment/baseline manifests, and recursive synthesis with either a
successful single repair or a terminal validation failure.

The first real-model synthetic trial failed to progress beyond synthesis; it is
retained as failure evidence, not called a successful E2E. A new trial is required
after the fix. Eight-hour endurance and real research-project acceptance remain
pending. A synthetic soak exercises repeated complete projects with scripted
roles; it is not evidence of eight hours of autonomous scientific research.
